"""Track the Python imports and search paths of each loaded plugin generation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.machinery import NamespaceLoader, PathFinder
from pathlib import Path
from types import ModuleType
from weakref import ref


@dataclass
class _SearchPath:
    users: int = 0
    inserted: str | None = None


_paths: dict[str, _SearchPath] = {}


class _RecordingLoader:
    def __init__(self, loader, owner: PluginImports):
        self._loader = loader
        self._owner = ref(owner)

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def exec_module(self, module: ModuleType) -> None:
        if isinstance(self._loader, NamespaceLoader):
            module.__file__ = None
        owner = self._owner()
        if owner is None:
            self._loader.exec_module(module)
        else:
            owner.execute(self._loader, module)


class _PluginPathFinder:
    """Delegate to the standard finder at its usual position in sys.meta_path."""

    def __init__(self) -> None:
        self.owners: dict[PluginImports, Path] = {}

    def find_spec(self, fullname, path=None, target=None):
        spec = PathFinder.find_spec(fullname, path, target)
        if target is not None or spec is None:
            return spec
        locations = spec.submodule_search_locations
        if spec.origin:
            origins = (Path(spec.origin).absolute(),)
        elif spec.loader is None and locations is not None:
            origins = tuple(Path(location).absolute() for location in locations)
        else:
            return spec

        # Prefer a matching relative namespace. For absolute imports, all
        # locations must belong to this root, including for namespace packages.
        owners = [
            owner for owner, root in tuple(self.owners.items())
            if origins and all(origin.is_relative_to(root) for origin in origins)
        ]
        if owners:
            owner = next((o for o in owners if fullname.startswith(o.namespace + ".")), owners[0])
            loader = spec.loader
            if loader is None and locations is not None:
                loader = NamespaceLoader(fullname, locations, PathFinder._get_spec)
            if hasattr(loader, "exec_module"):
                spec.loader = _RecordingLoader(loader, owner)
        return spec

    def __getattr__(self, name):
        # importlib.metadata also uses the standard finder's distribution API.
        return getattr(PathFinder, name)

    def add(self, owner: PluginImports, root: Path) -> None:
        if not self.owners and PathFinder in sys.meta_path:
            sys.meta_path[sys.meta_path.index(PathFinder)] = self
        self.owners[owner] = root

    def discard(self, owner: PluginImports) -> None:
        self.owners.pop(owner, None)
        if not self.owners and self in sys.meta_path:
            sys.meta_path[sys.meta_path.index(self)] = PathFinder


_finder = _PluginPathFinder()


def namespace_conflict(namespace: str, replacing: PluginImports | None = None) -> str | None:
    """Find a reserved namespace key this operation does not own."""
    owned = replacing.modules if replacing is not None else {}
    prefix = namespace + "."
    for key, module in tuple(sys.modules.items()):
        if (key == namespace or key.startswith(prefix)) and (key not in owned or owned[key] is not module):
            return key
    return None


class PluginImports:
    """Keep exact objects across unload, including for possible reload rollback."""

    def __init__(self, namespace: str, entry_dir: str):
        self.namespace = namespace
        self.path = str(Path(entry_dir).absolute()) if entry_dir else None
        self.modules: dict[str, ModuleType] = {}
        self._removed: dict[str, ModuleType] = {}
        self._attached = False

    def attach(self) -> None:
        self._attached = True
        if self.path is None:
            return
        use = _paths.setdefault(self.path, _SearchPath())
        use.users += 1
        if self.path not in sys.path:
            use.inserted = self.path
            sys.path.append(use.inserted)
        _finder.add(self, Path(self.path))

    def execute(self, loader, module: ModuleType) -> None:
        name = module.__name__
        loader.exec_module(module)
        if self._attached:
            self.modules[name] = module

    def detach(self) -> None:
        if not self._attached:
            return
        self._attached = False
        self._removed = {}
        shared_owner = next(
            (owner for owner in tuple(_finder.owners) if owner is not self and owner.path == self.path),
            None,
        )
        for name, module in self.modules.copy().items():
            if sys.modules.get(name) is not module:
                continue
            if shared_owner is not None and name != self.namespace and not name.startswith(self.namespace + "."):
                shared_owner.modules[name] = module
            else:
                self._removed[name] = sys.modules.pop(name)
        if self.path is None:
            return
        _finder.discard(self)
        use = _paths[self.path]
        use.users -= 1
        if not use.users:
            del _paths[self.path]
            if use.inserted is not None:
                for index, entry in enumerate(sys.path):
                    if entry is use.inserted:
                        del sys.path[index]
                        break

    def restore(self) -> bool:
        if namespace_conflict(self.namespace, self) is not None:
            return False
        if any(name in sys.modules and sys.modules[name] is not module for name, module in self._removed.items()):
            return False
        self.attach()
        sys.modules.update(self._removed)
        self._removed.clear()
        return True
