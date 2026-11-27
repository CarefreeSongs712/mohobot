from contextlib import ExitStack, contextmanager

_MISSING = object()


@contextmanager
def patched_attrs(target, **attrs):
    """Restore own attributes exactly, including missing and inherited names."""
    namespace = getattr(target, "__dict__", {})
    previous = {name: namespace.get(name, _MISSING) for name in attrs}
    try:
        for name, value in attrs.items():
            setattr(target, name, value)
        yield target
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                if name in getattr(target, "__dict__", {}):
                    delattr(target, name)
            else:
                setattr(target, name, value)


@contextmanager
def patched_items(mapping, **items):
    """Restore overwritten keys and remove keys introduced by the scope."""
    previous = {key: mapping.get(key, _MISSING) for key in items}
    try:
        mapping.update(items)
        yield mapping
    finally:
        for key, value in previous.items():
            if value is _MISSING:
                mapping.pop(key, None)
            else:
                mapping[key] = value


@contextmanager
def patched(*patches):
    """Apply attribute scopes in order and restore them in reverse order."""
    with ExitStack() as stack:
        for target, name, value in patches:
            stack.enter_context(patched_attrs(target, **{name: value}))
        yield
