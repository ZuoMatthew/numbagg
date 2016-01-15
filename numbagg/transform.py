import inspect
import re
import sys

PY2 = sys.version_info[0] < 3


def _func_globals(f):
    return f.func_globals if PY2 else f.__globals__


def _apply_source_transform(func, transform_source):
    """A horrible hack to make the syntax for writing aggregators more
    Pythonic.

    This should go away once numba is more fully featured.
    """
    orig_source = inspect.getsource(func)
    source = transform_source(orig_source)
    scope = {}
    exec(source, _func_globals(func), scope)
    try:
        return scope['__transformed_func']
    except KeyError:
        raise TypeError('failed to rewrite function definition:\n%s'
                        % orig_source)


def _transform_agg_source(func):
    """Transforms aggregation functions into something numba can handle.

    To be more precise, it converts functions with source that looks like

        @ndreduce
        def my_func(x)
            ...
            return foo

    into

        def __sub__gufunc(x, __out):
            ...
            __out[0] = foo
            return

    which is the form numba needs for writing a gufunc that returns a scalar
    value.
    """
    def transform_source(source):
        # nb. the right way to do this would be use Python's ast module instead
        # of regular expressions.
        source = re.sub(
            r'^@ndreduce[^\n]*\ndef\s+[a-zA-Z_][a-zA-Z_0-9]*\((.*?)\)\:',
            r'def __transformed_func(\1, __out):', source, flags=re.DOTALL)
        source = re.sub(r'(\s+)return\s+(.*)',
                        r'\1__out[0] = \2\n\1return', source)
        return source
    return _apply_source_transform(func, transform_source)


def _transform_moving_source(func):
    """Transforms moving aggregation functions into something numba can handle.
    """
    def transform_source(source):
        source = re.sub(
            r'^@ndmoving[^\n]*\ndef\s+[a-zA-Z_][a-zA-Z_0-9]*\((.*?)\)\:',
            r'def __transformed_func(\1):', source, flags=re.DOTALL)
        source = re.sub(r'^(\s+.*)(window)', r'\1window[0]', source,
                        flags=re.MULTILINE)
        return source
    return _apply_source_transform(func, transform_source)
