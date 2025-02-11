# Copyright The Caikit Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Most logic interacting with models.  Can load, etc.
"""

# Standard
from contextlib import contextmanager
from io import BytesIO
from threading import Lock
from typing import Union
import errno
import os
import tempfile
import zipfile

# First Party
import alog

# Local
from .module_backends import backend_types, module_backend_config
from .module_backends.base import SharedLoadBackendBase
from .modules.base import ModuleBase
from .modules.config import ModuleConfig
from .modules.decorator import SUPPORTED_LOAD_BACKENDS_VAR_NAME
from .registries import module_backend_registry, module_registry
from .toolkit.errors import error_handler
from caikit.config import get_config

log = alog.use_channel("MDLMNG")
error = error_handler.get(log)

# restrict functions that are imported so we don't pollute the base module namespace
__all__ = [
    "get_valid_module_ids",
    "ModelManager",
]


def get_valid_module_ids():
    """Get a dictionary mapping all module IDs to the string names of the
    implementing classes.
    """
    return {
        module_id: model_class.__name__
        for module_id, model_class in module_registry().items()
    }


class ModelManager:
    """Manage the models or resources for library."""

    def __init__(self):
        """Initialize ModelManager."""
        # Map to store module caches, to be used for singleton model lookups
        self.singleton_module_cache = {}
        self._singleton_lock = Lock()

    # make load function available from top-level of library
    def load(self, module_path, *args, load_singleton=False, **kwargs):
        """Load a model and return an instantiated object on which we can run inference.

        Args:
            module_path: str | BytesIO | bytes
                A module path to one of the following.
                    1. A module path to a directory containing a yaml config file in the top level.
                    2. A module path to a zip archive containing either a yaml config file in the
                       top level when extracted, or a directory containing a yaml config file in
                       the top level.
                    3. A BytesIO object corresponding to a zip archive containing either a yaml
                       config file in the top level when extracted, or a directory containing a
                       yaml config file in the top level.
                    4. A bytes object corresponding to a zip archive containing either a yaml
                       config file in the top level when extracted, or a directory containing a
                       yaml config file in the top level.
            load_singleton: bool (Defaults to False)
                Indicates whether this model should be loaded as a singleton.

        Returns:
            subclass of caikit.core.modules.ModuleBase
                Model object that is loaded, configured, and ready for prediction.
        """
        error.type_check("<COR98255724E>", bool, load_singleton=load_singleton)

        # This allows a user to load their own model (e.g. model saved to disk)
        load_path = get_config().load_path
        if load_path is not None and isinstance(module_path, str):
            if not os.path.exists(module_path):
                module_path = os.path.join(load_path, module_path)

        # Ensure that we have a loadable directory.
        error.type_check("<COR98255419E>", str, BytesIO, bytes, module_path=module_path)
        if isinstance(module_path, str):
            # Ensure this path is operating system correct if it isn't already.
            module_path = os.path.normpath(module_path)
        # If we have bytes, convert to a buffer, since we already handle in memory binary streams.
        elif isinstance(module_path, bytes):
            module_path = BytesIO(module_path)
        # Now that we have a file like object | str we can try to load as an archive.
        if zipfile.is_zipfile(module_path):
            return self._load_from_zipfile(module_path, load_singleton, *args, **kwargs)
        try:
            return self._load_from_dir(module_path, load_singleton, *args, **kwargs)
        except FileNotFoundError:
            error(
                "<COR80419785E>",
                FileNotFoundError(
                    "Module load path `{}` does not contain a `config.yml` file.".format(
                        module_path
                    )
                ),
            )

    def _load_from_dir(self, module_path, load_singleton, *args, **kwargs):
        """Load a model from a directory.

        Args:
            module_path:  str
                Path to directory. At the top level of directory is `config.yml` which holds info
                about the model.
            load_singleton: bool
                Indicates whether this model should be loaded as a singleton.

        Returns:
            subclass of caikit.core.modules.ModuleBase
                Model object that is loaded, configured, and ready for prediction.
        """
        # Short-circuit the loading process if the path does not exist
        if not os.path.exists(module_path):
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), module_path
            )

        # If this is a singleton load, the entire body of this function needs to
        # be locked to avoid concurrent loads on the same model. Otherwise, we
        # can freely load in parallel.
        with self.singleton_lock(load_singleton):

            # Using the module_path as a key, look for an instance preloaded in the
            # singleton cache if desired
            # 🌶🌶🌶 This doesn't work for nested modules
            # TODO: think about bringing back the `unique_hash` or `tracking_id`
            if singleton_entry := (
                load_singleton and self.singleton_module_cache.get(module_path)
            ):
                log.debug("Found %s in the singleton cache", module_path)
                return singleton_entry

            # Get the set of configured loaders
            configured_load_backends = module_backend_config.configured_load_backends()
            if not configured_load_backends:
                log.info(
                    "<COR56759744I>",
                    "No backends configured! Configuring backends with current configuration",
                )
                module_backend_config.configure()
                configured_load_backends = (
                    module_backend_config.configured_load_backends()
                )

            # Pre-initialize variables that will be parsed lazily from the
            # ModuleConfig if needed. This is done lazily so that loaders which
            # don't require a config.yml can take precedence over those that do
            # require one.
            module_id = None
            module_implementations = None
            model_creation_backend = None

            # For each backend, if it's a shared loader, attempt to load the model
            # directly. If not, parse the module config and look to see if there is
            # a version of the module available for the given backend
            loaded_model = None
            log.debug("Available load backends: %s", configured_load_backends)
            for i, load_backend in enumerate(configured_load_backends):
                # If this is a shared loader, try loading the model directly
                if isinstance(load_backend, SharedLoadBackendBase):
                    log.debug("Trying shared backend loader")
                    model = load_backend.load(module_path, *args, **kwargs)
                    if model is not None:
                        log.debug2(
                            "Successfully loaded %s with loader (%d)%s",
                            module_path,
                            i,
                            load_backend.backend_type,
                        )
                        error.type_check(
                            "<COR76726077E>",
                            ModuleBase,
                            model=model,
                        )

                        loaded_model = model
                        model.set_load_backend(load_backend)
                        break
                    log.debug3(
                        "Could not load %s with loader (%d)%s",
                        module_path,
                        i,
                        load_backend.backend_type,
                    )

                # If this is not a shared loader, look for an implementation of the
                # model's module that works with this backend
                else:
                    # If this is the first time parsing the module config, do so
                    if module_id is None:
                        log.debug2("Loading ModuleConfig from %s", module_path)
                        module_config = ModuleConfig.load(module_path)
                        module_id = module_config.module_id
                        module_implementations = module_backend_registry().get(
                            module_id, {}
                        )
                        log.debug2(
                            "Number of available backend implementations for %s found: %d",
                            module_id,
                            len(module_implementations),
                        )
                        # Look up the backend that this model was created with
                        model_creation_backend = module_config.get(
                            "model_backend", backend_types.LOCAL
                        )

                    # Look in the module's implementations for this backend type
                    backend_impl_obj = module_implementations.get(
                        load_backend.backend_type
                    )
                    if backend_impl_obj is None:
                        log.debug3(
                            "Module %s does not support loading with %s",
                            module_id,
                            load_backend.backend_type,
                        )
                        continue

                    # Grab the concrete module class for this backend and check to
                    # see if this model's artifacts were created with a version of
                    # the module that can be loaded with this backend.
                    module_backend_impl = backend_impl_obj.impl_class
                    supported_load_backends = self._get_supported_load_backends(
                        module_backend_impl
                    )
                    if model_creation_backend in supported_load_backends:
                        log.debug3(
                            "Attempting to load %s (module_id %s) with backend %s and class %s",
                            module_path,
                            module_id,
                            load_backend.backend_type,
                            module_backend_impl.__name__,
                        )
                        loaded_model = module_backend_impl.load(
                            module_path,
                            *args,
                            load_backend=load_backend,
                            **kwargs,
                        )
                        if loaded_model is not None:
                            log.debug2(
                                "Successfully loaded %s with backend %s",
                                module_path,
                                load_backend.backend_type,
                            )
                            loaded_model.set_load_backend(load_backend)
                            break

            # If no model successfully loaded, it's an error
            if loaded_model is None:
                error(
                    "<COR50207494E>",
                    ValueError(
                        f"Unable to load model from {module_path} with MODULE_ID {module_id}"
                    ),
                )

            # If loading as a singleton, populate the cache
            if load_singleton:
                self.singleton_module_cache[module_path] = loaded_model

            # Return successfully!
            return loaded_model

    def _load_from_zipfile(self, module_path, load_singleton, *args, **kwargs):
        """Load a model from a zip archive.

        Args:
            module_path:  str
                Path to directory. At the top level of directory is `config.yml` which holds info
                about the model.
            load_singleton: bool
                Indicates whether this model should be loaded as a singleton.

        Returns:
            subclass of caikit.core.modules.ModuleBase
                Model object that is loaded, configured, and ready for prediction.
        """
        with tempfile.TemporaryDirectory() as extract_path:
            with zipfile.ZipFile(module_path, "r") as zip_f:
                zip_f.extractall(extract_path)
            # Depending on the way the zip archive is packaged, out temp directory may unpack
            # to files directly, or it may unpack to a (single) directory containing the files.
            # We expect the former, but fall back to the second if we can't find the config.
            try:
                model = self._load_from_dir(
                    extract_path, load_singleton, *args, **kwargs
                )
            # NOTE: Error handling is a little gross here, the main reason being that we
            # only want to log to error() if something is fatal, and there are a good amount
            # of things that can go wrong in this process.
            except FileNotFoundError:

                def get_full_path(folder_name):
                    return os.path.join(extract_path, folder_name)

                # Get the contained directories. Omit anything starting with __ to avoid
                # accidentally traversing compression artifacts, e.g., __MACOSX.
                nested_dirs = [
                    get_full_path(f)
                    for f in os.listdir(extract_path)
                    if os.path.isdir(get_full_path(f)) and not f.startswith("__")
                ]
                # If we have multiple dirs, something is probably wrong - this doesn't look
                # like a simple level of nesting as a result of creating the zip.
                if len(nested_dirs) != 1:
                    error(
                        "<COR06761097E>",
                        FileNotFoundError(
                            "Unable to locate archive config due to nested dirs"
                        ),
                    )
                # Otherwise, try again. If we fail again stop, because the zip creation should only
                # create one potential extra layer of nesting around the model directory.
                try:
                    model = self._load_from_dir(
                        nested_dirs[0], load_singleton, *args, **kwargs
                    )
                except FileNotFoundError:
                    error(
                        "<COR84410081E>",
                        FileNotFoundError(
                            "Unable to locate archive config within top two levels of {}".format(
                                module_path
                            )
                        ),
                    )
        return model

    def extract(self, zip_path, model_path, force_overwrite=False):
        """Method to extract a downloaded archive to a specified directory.

        Args:
            zip_path: str
                Location of .zip file to extract.
            model_path: str
                Model directory where the archive should be unzipped unzipped.
            force_overwrite: bool (Defaults to false)
                Force an overwrite to model_path, even if the folder exists
        Returns:
            str
                Output path where the model archive is unzipped.
        """
        model_path = os.path.abspath(model_path)

        # skip if force_overwrite disabled and path already exists
        if not force_overwrite and os.path.exists(model_path):
            log.info(
                "INFO: Skipped extraction. Archive already extracted in directory: %s",
                model_path,
            )
            return model_path

        with zipfile.ZipFile(zip_path, "r") as zip_f:
            zip_f.extractall(model_path)

        # path to model
        return model_path

    def resolve_and_load(
        self, path_or_name_or_model_reference: Union[str, ModuleBase], **kwargs
    ):
        """Try our best to load a model, given a path or a name. Simply returns any loaded model
        passed in. This exists to ease the burden on workflow developers who need to accept
        individual modules in their API, where users may have references to custom models or may
        only have the ability to give the name of a stock model.

        Args:
            path_or_name_or_model_reference (str, ModuleBase): Either a
                - Path to a model on disk
                - Name of a model that the catalog knows about
                - Loaded module
            **kwargs: Any keyword arguments to pass along to ModelManager.load()
                      or ModelManager.download()
                e.g. parent_dir

        Returns:
            A loaded module

        Examples:
            >>> stock_syntax_model = manager.resolve_and_load('syntax_izumo_en_stock')
            >>> local_categories_model = manager.resolve_and_load('path/to/categories/model')
            >>> some_custom_model = manager.resolve_and_load(some_custom_model)
        """
        error.type_check(
            "<COR50266694E>",
            str,
            ModuleBase,
            path_or_name_or_model_reference=path_or_name_or_model_reference,
        )

        # If this is already a module, we're good to go
        if isinstance(path_or_name_or_model_reference, ModuleBase):
            log.debug("Returning model %s directly", path_or_name_or_model_reference)
            return path_or_name_or_model_reference

        # Otherwise, this could either be a path on disk or some name of a model that our catalog
        # can resolve and fetch
        if os.path.isdir(path_or_name_or_model_reference):
            # Try to load from path
            log.debug(
                "Attempting to load model from path %s", path_or_name_or_model_reference
            )
            return self.load(path_or_name_or_model_reference, **kwargs)

        error(
            "<COR50207495E>",
            ValueError(
                "could not find model with name `{}`".format(
                    path_or_name_or_model_reference
                )
            ),
        )

    def get_singleton_model_cache_info(self):
        """Returns information about the singleton cache in {hash: module type} format

        Returns:
            Dict[str, type]
                A dictionary of model hashes to model types
        """
        return {k: type(v) for k, v in self.singleton_module_cache.items()}

    def clear_singleton_cache(self):
        """Clears the cache of singleton models. Useful to release references of models, as long as
        you know that they are no longer held elsewhere and you won't be loading them again.

        Returns:
            None
        """
        with self._singleton_lock:
            self.singleton_module_cache.clear()

    @contextmanager
    def singleton_lock(self, load_singleton: bool):
        """Helper contextmanager that will only lock the singleton cache if this
        load is a singleton load
        """
        if load_singleton:
            with self._singleton_lock:
                yield
        else:
            yield

    def _get_supported_load_backends(self, backend_impl: ModuleBase):
        """Function to get a list of supported load backends
        that the module supports

        Args:
            backend_impl: caikit.core.ModuleBase
                Module implementing the backend
        Returns:
            list(backend_types)
                list of backends that are supported for model load
        """

        # Get list of backends that are supported for load
        # NOTE: since code in a module can change anytime, its support
        # for various backend might also change, in which case,
        # it would be better to keep the backend information in the model itself
        # If module_backend is None, then we will assume that this model is not loadable in
        # any other backend
        return getattr(backend_impl, SUPPORTED_LOAD_BACKENDS_VAR_NAME, [])
