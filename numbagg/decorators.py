import inspect
import re

import numba
import numpy as np

from .cache import cached_property, FunctionCache
from .transform import _transform_agg_source, _transform_moving_source


def _nd_func_maker(cls, arg, **kwargs):
    if callable(arg) and not kwargs:
        return cls(arg)
    else:
        return lambda func: cls(func, signature=arg, **kwargs)


def ndreduce(*args, **kwargs):
    """Turn a function the aggregates an array into a single value, into a
    multi-dimensional aggregation function accelerated by numba.
    """
    return _nd_func_maker(NumbaNDReduce, *args, **kwargs)


def ndmoving(*args, **kwargs):
    """Accelerate a moving window function.
    """
    return _nd_func_maker(NumbaNDMoving, *args, **kwargs)


def _validate_axis(axis, ndim):
    """Helper function to convert axis into a non-negative integer, or raise if
    it's invalid.
    """
    if axis < 0:
        axis += ndim
    if axis < 0 or axis >= ndim:
        raise ValueError('invalid axis %s' % axis)
    return axis


class NumbaNDReduce(object):
    def __init__(self, func, signature=('float32(float32)',
                                        'float64(float64)')):
        self.func = func
        self.signature = signature
        self._jit_cache = FunctionCache(self._create_jit)
        self._gufunc_cache = FunctionCache(self._create_gufunc)

    @property
    def __name__(self):
        return self.func.__name__

    def __repr__(self):
        return '<numbagg.decorators.NumbaNDReduce %s>' % self.__name__

    @cached_property
    def transformed_func(self):
        return _transform_agg_source(self.func)

    def _parsed_signature(self, core_ndim):
        colons = ','.join(':' for _ in range(core_ndim))
        colons = '[%s]' % colons if colons else ''
        for signature in self.signature:
            match = re.match('^(\w+)\((\w+)\)$', signature)
            if not match:
                raise ValueError('invalid signature')
            out_dtype, in_dtype = match.groups()
            yield colons, in_dtype, out_dtype


        # colons = ','.join(':' for _ in range(max(core_ndim, 1)))

        # def parse_sig(signature):
        #     match = re.match('^(\w+)\((\w+)\)$', signature)
        #     if not match:
        #         raise ValueError('invalid signature')
        #     out_dtype, in_dtype = match.groups()
        #     return '%s[%s]' % (in_dtype, colons), out_dtype

        # return [parse_sig(sig) for sig in self.signature]

    def _create_jit(self, core_ndim):
        numba_sig = ['%s(%s%s)' % (out_dtype, in_dtype, colons)
                     for colons, in_dtype, out_dtype
                     in self._parsed_signature(core_ndim)]
        vectorize = numba.jit(numba_sig, nopython=True, nogil=True)
        return vectorize(self.func)

    def _create_gufunc(self, core_ndim):
        # creating compiling gufunc has some significant overhead (~130ms per
        # function and number of dimensions to aggregate), so do this in a
        # lazy fashion
        numba_sig = ['void(%s%s, %s[:])' % (in_dtype,
                                            colons if colons else '[:]',
                                            out_dtype)
                     for colons, in_dtype, out_dtype
                     in self._parsed_signature(core_ndim)]
        # parsed_signature = self._parse_signature(core_ndim)
        # numba_sig = ['void(%s, %s[:])' % args for args in parsed_signature]

        gufunc_sig = '(%s)->()' % ','.join(list('abcdefgijk')[:core_ndim])
        vectorize = numba.guvectorize(numba_sig, gufunc_sig, nopython=True)
        return vectorize(self.transformed_func)

    def __call__(self, arr, axis=None):
        if axis is None:
            # TODO: switch to using jit_func (it's faster), once numba reliably
            # returns the right dtype
            # see: https://github.com/numba/numba/issues/1087
            # f = self._jit_func
            f = self._jit_cache[arr.ndim]
        elif np.isscalar(axis):
            axis = _validate_axis(axis, arr.ndim)
            all_axes = [n for n in range(arr.ndim) if n != axis] + [axis]
            arr = arr.transpose(all_axes)
            f = self._gufunc_cache[1]
        else:
            axis = [_validate_axis(a, arr.ndim) for a in axis]
            all_axes = [n for n in range(arr.ndim)
                        if n not in axis] + list(axis)
            arr = arr.transpose(all_axes)
            f = self._gufunc_cache[len(axis)]
        return f(arr)


MOVE_WINDOW_ERR_MSG = "invalid window (not between 1 and %d, inclusive): %r"


class NumbaNDMoving(object):
    def __init__(self, func, signature=['float64,int64,float64']):
        self.func = func
        self.signature = signature

    @cached_property
    def transformed_func(self):
        return _transform_moving_source(self.func)

    @cached_property
    def gufunc(self):
        extra_args = len(inspect.getargspec(self.func).args) - 2
        dtype_str = ['void(%s)' % ','.join('%s[:]' % e
                                           for e in d.split(','))
                     for d in self.signature]
        sig = '(n)%s->(n)' % ''.join(',()' for _ in range(extra_args))
        vectorize = numba.guvectorize(dtype_str, sig, nopython=True)
        return vectorize(self.transformed_func)

    def __call__(self, arr, window, axis=-1):
        axis = _validate_axis(axis, arr.ndim)
        window = np.asarray(window)
        # TODO: test this validation
        if (window < 1).any() or (window > arr.shape[axis]).any():
            raise ValueError(MOVE_WINDOW_ERR_MSG % (arr.shape[axis], window))
        arr = arr.swapaxes(axis, -1)
        return self.gufunc(arr, window)
