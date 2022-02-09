# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import enum
from dataclasses import dataclass
from functools import partial
import itertools as it
from typing import Union, Optional, Callable, Dict, Tuple, TypeVar, FrozenSet

import numpy as np

import jax.numpy as jnp

from jax import core
from jax import linear_util as lu
from jax.api_util import flatten_fun
from jax.interpreters import partial_eval as pe
from jax.tree_util import tree_flatten, tree_unflatten, register_pytree_node
from jax._src import source_info_util, traceback_util
from jax import lax
from jax._src.util import (as_hashable_function, unzip2, split_list, safe_map,
                           safe_zip)

source_info_util.register_exclusion(__file__)
traceback_util.register_exclusion(__file__)

map, unsafe_map = safe_map, map
zip, unsafe_zip = safe_zip, zip


## Utils

def popattr(obj, attrname):
  val = getattr(obj, attrname)
  delattr(obj, attrname)
  return val

def setnewattr(obj, name, val):
  sentinel = object()
  assert getattr(obj, name, sentinel) is sentinel
  setattr(obj, name, val)


## Error value data type and functional assert.

Bool = Union[bool, core.Tracer]
Int = Union[int, core.Tracer]


@dataclass(frozen=True)
class Error:
  err: Bool
  code: Int
  msgs: Dict[int, str]

  def get(self) -> Optional[str]:
    """Returns error message is error happened, None if no error happened."""
    assert np.shape(self.err) == np.shape(self.code)
    if np.size(self.err) == 1:
      if self.err:
        return self.msgs[int(self.code)]
    else:
      return '\n'.join(f'at mapped index {", ".join(map(str, idx))}: '  # type: ignore
                       f'{self.msgs[int(self.code[idx])]}'              # type: ignore
                       for idx, e in np.ndenumerate(self.err) if e) or None
    return None

  def throw(self):
    """Throw ValueError with error message if error happened."""
    err = self.get()
    if err:
      raise ValueError(err)

register_pytree_node(Error,
                     lambda e: ((e.err, e.code), tuple(sorted(e.msgs.items()))),
                     lambda msgs, data: Error(*data, dict(msgs)))  # type: ignore

init_error = Error(False, 0, {})
next_code = it.count(1).__next__  # globally unique ids, could be uuid4


def assert_func(error: Error, pred: Bool, msg: str) -> Error:
  code = next_code()
  out_err = error.err | jnp.logical_not(pred)
  out_code = lax.select(error.err, error.code, code)
  return Error(out_err, out_code, {code: msg, **error.msgs})


## Checkify transformation for plumbing functional error values.

class CheckifyTracer(core.Tracer):
  def __init__(self, trace, val):
    self._trace = trace
    self.val = val
    core.get_aval(val), val
  aval = property(lambda self: core.get_aval(self.val))
  full_lower = lambda self: self

class CheckifyTrace(core.Trace):
  pure = lift = lambda self, val: CheckifyTracer(self, val)

  def __init__(self, main: core.MainTrace, sublevel: core.Sublevel,
               enabled_errors: FrozenSet['ErrorCategory']) -> None:
    self.main = main
    self.level = main.level
    self.sublevel = sublevel
    self.main.enabled_errors = enabled_errors

  def sublift(self, tracer):
    return CheckifyTracer(self, tracer.val)

  def process_primitive(self, primitive, tracers, params):
    in_vals = [t.val for t in tracers]
    rule = error_checks.get(primitive)
    if rule:
      out, self.main.error = rule(self.main.error, self.main.enabled_errors,  # type: ignore
                                  *in_vals, **params)
    else:
      out = primitive.bind(*in_vals, **params)
    if primitive.multiple_results:
      return [CheckifyTracer(self, x) for x in out]
    else:
      return CheckifyTracer(self, out)

  def process_call(self, primitive, f, tracers, params):
    in_vals = [t.val for t in tracers]
    e = popattr(self.main, 'error')
    f, msgs = checkify_subtrace(f, self.main, tuple(e.msgs.items()))
    params_ = dict(params, donated_invars=(False, False, *params['donated_invars']))
    err, code, *out_vals = primitive.bind(f, e.err, e.code, *in_vals, **params_)
    setnewattr(self.main, 'error', Error(err, code, msgs()))
    return [CheckifyTracer(self, x) for x in out_vals]

  def process_map(self, primitive, f, tracers, params):
    in_vals = [t.val for t in tracers]
    e = popattr(self.main, 'error')
    f, msgs = checkify_subtrace(f, self.main, tuple(e.msgs.items()))

    @as_hashable_function(closure=params['out_axes_thunk'])
    def new_out_axes_thunk():
      return (0, 0, *params['out_axes_thunk']())

    params_ = dict(params, in_axes=(None, None, *params['in_axes']),
                   out_axes_thunk=new_out_axes_thunk,
                   donated_invars=(False, False, *params['donated_invars']))
    errs, codes, *outs = primitive.bind(f, e.err, e.code, *in_vals, **params_)
    err, code = _reduce_any_error(errs, codes)
    setnewattr(self.main, 'error', Error(err, code, msgs()))
    return [CheckifyTracer(self, x) for x in outs]

  def post_process_call(self, primitive, tracers, params):
    vals = [t.val for t in tracers]
    main = self.main
    e = popattr(main, 'error')
    err, code, main.msgs = e.err, e.code, e.msgs
    def todo(vals):
      err, code, *vals = vals
      setnewattr(main, 'error', Error(err, code, popattr(main, 'msgs')))
      trace = main.with_cur_sublevel()
      return [CheckifyTracer(trace, x) for x in vals]
    return (err, code, *vals), todo

  def post_process_map(self, primitive, tracers, params):
    vals = [t.val for t in tracers]
    main = self.main
    e = popattr(main, 'error')
    err, code, main.msgs = e.err, e.code, e.msgs
    def todo(vals):
      errs, codes, *vals = vals
      err, code = _reduce_any_error(errs, codes)
      setnewattr(main, 'error', Error(err, code, popattr(main, 'msgs')))
      trace = main.with_cur_sublevel()
      return [CheckifyTracer(trace, x) for x in vals]
    def out_axes_transform(out_axes):
      return (0, 0, *out_axes)
    return (err, code, *vals), (todo, out_axes_transform)

  def process_custom_jvp_call(self, prim, fun, jvp, tracers):
    in_vals = [t.val for t in tracers]
    e = popattr(self.main, 'error')
    msgs = tuple(e.msgs.items())
    fun, msgs1 = checkify_subtrace(fun, self.main, msgs)
    jvp, msgs2 = checkify_custom_jvp_subtrace(jvp, self.main, msgs)
    err, code, *out_vals = prim.bind(fun, jvp, e.err, e.code, *in_vals)
    fst, out_msgs = lu.merge_linear_aux(msgs1, msgs2)
    setattr(self.main, 'error', Error(err, code, out_msgs))
    return [CheckifyTracer(self, x) for x in out_vals]

  def post_process_custom_jvp_call(self, out_tracers, jvp_was_run):
    if jvp_was_run:
      msg = ("support for custom_jvp rules which close over checkify values is "
             "not implemented. If you see this, open an issue at "
             "https://github.com/google/jax/issues!")
      raise NotImplementedError(msg)
    vals = [t.val for t in out_tracers]
    main = self.main
    e = popattr(main, 'error')
    err, code, main.msgs = e.err, e.code, e.msgs
    def todo(vals):
      err, code, *vals = vals
      setnewattr(main, 'error', Error(err, code, popattr(main, 'msgs')))
      trace = main.with_cur_sublevel()
      return [CheckifyTracer(trace, x) for x in vals]
    return (err, code, *vals), todo

def _reduce_any_error(errs, codes):
  errs_, codes_ = lax.sort_key_val(errs, codes, dimension=0)
  return errs_[-1], codes_[-1]

ErrorCheckRule = Callable  # (Error, FrozenSet[ErrorCategory], *in_vals, **params) -> (Any, Error)
error_checks: Dict[core.Primitive, ErrorCheckRule] = {}

def checkify_flat(fun: lu.WrappedFun, enabled_errors: FrozenSet['ErrorCategory'],
                  *args):
  fun, msgs = checkify_subtrace(fun)
  fun = checkify_traceable(fun, tuple(init_error.msgs.items()), enabled_errors)
  err, code, *outvals = fun.call_wrapped(init_error.err, init_error.code, *args)
  return (err, code, outvals), msgs()

@lu.transformation
def checkify_traceable(msgs, enabled_errors, err, code, *args):
  with core.new_main(CheckifyTrace, enabled_errors=enabled_errors) as main:
    outs = yield (main, msgs, err, code, *args), {}
    del main
  yield outs

@lu.transformation_with_aux
def checkify_subtrace(main, msgs, err, code, *args):
  setnewattr(main, 'error', Error(err, code, dict(msgs)))
  trace = main.with_cur_sublevel()
  in_tracers = [CheckifyTracer(trace, x) for x in args]
  out = yield in_tracers, {}
  out_tracers = map(trace.full_raise, out)
  out_vals = [t.val for t in out_tracers]
  err, code, msgs = main.error.err, main.error.code, main.error.msgs
  del main.error
  yield (err, code, *out_vals), msgs

@lu.transformation_with_aux
def checkify_custom_jvp_subtrace(main, msgs, *args):
  # Like checkify_subtrace, but used specifically on the custom JVP rules
  # associated with a custom_jvp. This code is called in the context of a
  # jvp-of-checkify-of-custom_jvp. It takes both primal and tangent inputs,
  # flattened into a single args tuple, and similarly must produce flattened
  # primal and tangent outputs. Both primals and tangents include error values,
  # but the tangent error values are trivially zero.
  # The types to have in mind are:
  #   jvp : (a -> b) -> (a, T a) -> (b, T b)
  #   checkify : (a -> b) -> a -> Err b
  #   jvp-of-checkify : (a -> b) -> (a, T a) -> (Err b, T (Err b))
  # where because Err is a pytree, we necessarily have T (Err b) = Err' (T b)
  # where the other Err' components are trivial (of float0 dtype).
  # Semantically, we don't add checks to the JVP rule. To check the result of a
  # JVP rule, one must instead use checkify-of-jvp. Thus this implementation
  # just forwards the input error and code (and trivial tangents) to the output.
  n, ragged = divmod(len(args), 2)
  assert not ragged
  (err,), (code,), primals = split_list(args[:n], [1, 1])
  (err_dot,), (code_dot,), tangents = split_list(args[n:], [1, 1])
  outs = yield (*primals, *tangents), {}
  m, ragged = divmod(len(outs), 2)
  assert not ragged
  out_primals, out_tangents = outs[:m], outs[m:]
  yield (err, code, *out_primals, err_dot, code_dot, *out_tangents), dict(msgs)

# TODO take (error_aval, code_aval) instead of error here?
def checkify_jaxpr(jaxpr, error, enabled_errors):
  f = lu.wrap_init(core.jaxpr_as_fun(jaxpr))
  return checkify_fun_to_jaxpr(f, error, enabled_errors, jaxpr.in_avals)

def checkify_fun_to_jaxpr(f, error, enabled_errors, in_avals):
  f, msgs = checkify_subtrace(f)
  f = checkify_traceable(f, tuple(error.msgs.items()), enabled_errors)
  err_aval = core.raise_to_shaped(core.get_aval(error.err))
  code_aval = core.raise_to_shaped(core.get_aval(error.code))
  avals_in = [err_aval, code_aval, *in_avals]
  jaxpr_out, _, literals_out = pe.trace_to_jaxpr_dynamic(f, avals_in)
  return core.ClosedJaxpr(jaxpr_out, literals_out), msgs()


## assert primitive

def check(pred: Bool, msg: str) -> None:
  """Check a condition, add an error with msg if condition is False.

  This is an effectful operation, and can't be staged (jitted/scanned/...).
  Before staging a function with checks, ``checkify`` it!

  Args:
    pred: if False, an error is added.
    msg: error message if error is added.

  For example:

    >>> import jax
    >>> import jax.numpy as jnp
    >>> from jax.experimental import checkify
    >>> def f(x):
    ...   checkify.check(x!=0, "cannot be zero!")
    ...   return 1/x
    >>> checked_f = checkify.checkify(f)
    >>> err, out = jax.jit(checked_f)(0)
    >>> err.throw()  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: cannot be zero! (check failed at ...)

  """
  if not is_scalar_pred(pred):
    raise TypeError(f'check takes a scalar pred as argument, got {pred}')
  code = next_code()
  msg += f' (check failed at {summary()})'
  return check_error(Error(jnp.logical_not(pred), code, {code: msg}))

def is_scalar_pred(pred) -> bool:
  return (isinstance(pred, bool) or
          isinstance(pred, jnp.ndarray) and pred.shape == () and
          pred.dtype == jnp.dtype('bool'))

def check_error(error: Error) -> None:
  """Raise an Exception if `error` represents a failure. Functionalized by `checkify`.

  The semantics of this function are equivalent to:

    def check_error(err: Error) -> None:
      err.throw()  # can raise ValueError Exception

  But unlike that implementation, `check_error` can be functionalized using
  the `checkify` transformation.

  This function is similar to `check` but with a different signature: whereas
  `check` takes as arguments a boolean predicate and a new error message
  string, this function takes an `Error` value as argument. Both `check` and
  this function raise a Python Exception on failure (a side-effect), and thus
  cannot be staged out by `jit`, `pmap`, `scan`, etc. Both also can be
  functionalized by using `checkify`.

  But unlike `check`, this function is like a direct inverse of `checkify`:
  whereas `checkify` takes as input a function which can raise a Python
  Exception and produces a new function without that effect but which
  produces an `Error` value as output, this `check_error` function can accept
  an `Error` value as input and can produce the side-effect of raising an
  Exception. That is, while `checkify` goes from functionalizable Exception
  effect to error value, this `check_error` goes from error value to
  functionalizable Exception effect.

  `check_error` is useful when you want to turn checks represented by an
  `Error` value (produced by functionalizing `check`s via `checkify`) back
  into Python Exceptions.

  Args:
    error: Error to check

  For example:

  >>> import jax
  >>> from jax.experimental import checkify
  >>> def f(x):
  ...   checkify.check(x>0, "must be positive!")
  ...   return x
  >>> def with_inner_jit(x):
  ...   checked_f = checkify.checkify(f)
  ...   # a checkified function can be jitted
  ...   error, out = jax.jit(checked_f)(x)
  ...   checkify.check_error(error)
  ...   return out
  >>> _ = with_inner_jit(1)  # no failed check
  >>> with_inner_jit(-1)  # doctest: +IGNORE_EXCEPTION_DETAIL
  Traceback (most recent call last):
    ...
  ValueError: must be positive!
  >>> # can re-checkify
  >>> error, _ = checkify.checkify(with_inner_jit)(-1)
  """
  return assert_p.bind(~error.err, error.code, msgs=error.msgs)

assert_p = core.Primitive('assert') # TODO: rename to check?
assert_p.multiple_results = True  # zero results

@assert_p.def_impl
def assert_impl(pred, code, *, msgs):
  Error(~pred, code, msgs).throw()
  return []

@assert_p.def_abstract_eval
def assert_abstract_eval(pred, code, *, msgs):
  raise Exception("can't be staged!")


## checkify rules

def summary() -> str:
  return str(source_info_util.summarize(source_info_util.current()))

def nan_error_check(prim, error, enabled_errors, *in_vals, **params):
  out = prim.bind(*in_vals, **params)
  if ErrorCategory.NAN not in enabled_errors:
    return out, error
  no_nans = jnp.logical_not(jnp.any(jnp.isnan(out)))
  msg = f"nan generated by primitive {prim.name} at {summary()}"
  return out, assert_func(error, no_nans, msg)

def gather_error_check(error, enabled_errors, operand, start_indices, *,
                       dimension_numbers, slice_sizes, unique_indices,
                       indices_are_sorted, mode, fill_value):
  out = lax.gather_p.bind(
      operand, start_indices, dimension_numbers=dimension_numbers,
      slice_sizes=slice_sizes, unique_indices=unique_indices,
      indices_are_sorted=indices_are_sorted, mode=mode, fill_value=fill_value)

  if ErrorCategory.OOB not in enabled_errors:
    return out, error

  # compare to OOB masking logic in lax._gather_translation_rule
  dnums = dimension_numbers
  operand_dims = np.array(operand.shape)

  upper_bound = operand_dims[np.array(dnums.start_index_map)]
  upper_bound -= np.array(slice_sizes)[np.array(dnums.start_index_map)]
  all_inbounds = jnp.all((start_indices >= 0) & (start_indices <= upper_bound))

  msg = f"out-of-bounds indexing at {summary()}"
  return out, assert_func(error, all_inbounds, msg)
error_checks[lax.gather_p] = gather_error_check

def div_error_check(error, enabled_errors, x, y):
  """Checks for division by zero and NaN."""
  if ErrorCategory.DIV in enabled_errors:
    all_nonzero = jnp.logical_not(jnp.any(jnp.equal(y, 0)))
    msg = f'divided by zero at {summary()}'
    error = assert_func(error, all_nonzero, msg)
  return nan_error_check(lax.div_p, error, enabled_errors, x, y)
error_checks[lax.div_p] = div_error_check

def scatter_in_bounds(operand, indices, updates, dnums):
  # Ref: see clamping code used in scatter_translation_rule
  slice_sizes = []
  pos = 0
  for i in range(len(operand.shape)):
    if i in dnums.inserted_window_dims:
      slice_sizes.append(1)
    else:
      slice_sizes.append(updates.shape[dnums.update_window_dims[pos]])
      pos += 1

  upper_bound = np.array([operand.shape[i] - slice_sizes[i]
                          for i in dnums.scatter_dims_to_operand_dims],
                         np.int64)
  upper_bound = np.minimum(upper_bound, np.iinfo(indices.dtype).max)
  upper_bound = lax.broadcast_in_dim(upper_bound, indices.shape,
                                     (len(indices.shape) - 1,))

  lower_in_bounds = jnp.all(jnp.greater_equal(indices, 0))
  upper_in_bounds = jnp.all(jnp.less_equal(indices, upper_bound))
  return jnp.logical_and(lower_in_bounds, upper_in_bounds)

def scatter_error_check(prim, error, enabled_errors, operand, indices, updates,
                        *, update_jaxpr, update_consts, dimension_numbers,
                        indices_are_sorted, unique_indices, mode):
  """Checks if indices are within bounds and update does not generate NaN."""
  out = prim.bind(
      operand, indices, updates, update_jaxpr=update_jaxpr,
      update_consts=update_consts, dimension_numbers=dimension_numbers,
      indices_are_sorted=indices_are_sorted, unique_indices=unique_indices,
      mode=mode)

  if ErrorCategory.OOB not in enabled_errors:
    return out, error

  in_bounds = scatter_in_bounds(operand, indices, updates, dimension_numbers)
  oob_msg = f'out-of-bounds indexing while updating at {summary()}'
  oob_error = assert_func(error, in_bounds, oob_msg)

  no_nans = jnp.logical_not(jnp.any(jnp.isnan(out)))
  nan_msg = f'nan generated by primitive {prim.name} at {summary()}'
  return out, assert_func(oob_error, no_nans, nan_msg)
error_checks[lax.scatter_p] = partial(scatter_error_check, lax.scatter_p)
error_checks[lax.scatter_add_p] = partial(scatter_error_check, lax.scatter_add_p)
error_checks[lax.scatter_mul_p] = partial(scatter_error_check, lax.scatter_mul_p)
error_checks[lax.scatter_min_p] = partial(scatter_error_check, lax.scatter_min_p)
error_checks[lax.scatter_max_p] = partial(scatter_error_check, lax.scatter_max_p)

def cond_error_check(error, enabled_errors, index, *ops, branches, linear):
  new_branches, msgs_ = unzip2(checkify_jaxpr(jxpr, error, enabled_errors)
                               for jxpr in branches)
  new_linear = (False, False, *linear)
  err, code, *outs = lax.cond_p.bind(
      index, error.err, error.code, *ops,
      branches=tuple(new_branches), linear=new_linear)
  new_msgs = {k:v for d in it.chain([error.msgs], msgs_) for k, v in d.items()}
  return outs, Error(err, code, new_msgs)
error_checks[lax.cond_p] = cond_error_check

def scan_error_check(error, enabled_errors, *in_flat, reverse, length, jaxpr,
                     num_consts, num_carry, linear, unroll):
  consts, carry, xs = split_list(in_flat, [num_consts, num_carry])
  checked_jaxpr, msgs_ = checkify_jaxpr(jaxpr, error, enabled_errors)
  new_linear = (False, False, *linear)
  new_in_flat = [*consts, error.err, error.code, *carry, *xs]
  err, code, *outs = lax.scan_p.bind(
      *consts, *new_in_flat,
      reverse=reverse, length=length, jaxpr=checked_jaxpr,
      num_consts=len(consts), num_carry=len(carry)+2,
      linear=new_linear, unroll=unroll)
  new_msgs = {**error.msgs, **msgs_}
  return outs, Error(err, code, new_msgs)
error_checks[lax.scan_p] = scan_error_check

def checkify_while_body_jaxpr(cond_jaxpr, body_jaxpr, error, enabled_errors):
  cond_f = core.jaxpr_as_fun(cond_jaxpr)
  body_f = core.jaxpr_as_fun(body_jaxpr)
  def new_body_f(*vals):
    out = body_f(*vals)
    _ = cond_f(*out)  # this checks if the next cond application will error
    return out
  return checkify_fun_to_jaxpr(lu.wrap_init(new_body_f), error, enabled_errors,
                               body_jaxpr.in_avals)

def ignore_errors_jaxpr(jaxpr, error):
  """Constructs a jaxpr which takes two extra args but ignores them."""
  err_aval = core.raise_to_shaped(core.get_aval(error.err))
  code_aval = core.raise_to_shaped(core.get_aval(error.code))
  consts = jaxpr.consts
  jaxpr = jaxpr.jaxpr
  new_vars = core.gensym([jaxpr])
  new_invars = (new_vars(err_aval), new_vars(code_aval), *jaxpr.invars)
  new_jaxpr = core.Jaxpr(jaxpr.constvars, new_invars,
                         jaxpr.outvars, jaxpr.eqns)
  return core.ClosedJaxpr(new_jaxpr, consts)

def while_loop_error_check(error, enabled_errors, *in_flat, cond_nconsts,
                           cond_jaxpr, body_nconsts, body_jaxpr):
  cond_jaxpr_, msgs_cond = checkify_jaxpr(cond_jaxpr, error, enabled_errors)
  checked_cond_fun = core.jaxpr_as_fun(cond_jaxpr_)
  # Check if the first cond application will error.
  cond_err, cond_code, _ = checked_cond_fun(error.err, error.code, *in_flat)

  checked_body_jaxpr, msgs_body = checkify_while_body_jaxpr(
    cond_jaxpr, body_jaxpr, error, enabled_errors)
  compat_cond_jaxpr = ignore_errors_jaxpr(cond_jaxpr, error)
  c_consts, b_consts, carry = split_list(in_flat, [cond_nconsts, body_nconsts])
  new_in_flat = [*c_consts, *b_consts, cond_err, cond_code, *carry]
  err, code, *out = lax.while_p.bind(
      *new_in_flat,
      cond_nconsts=cond_nconsts,
      cond_jaxpr=compat_cond_jaxpr,
      body_nconsts=body_nconsts,
      body_jaxpr=checked_body_jaxpr)
  new_msgs = {**error.msgs, **msgs_body, **msgs_cond}
  return out, Error(err, code, new_msgs)
error_checks[lax.while_p] = while_loop_error_check

def add_nan_check(prim):
  error_checks[prim] = partial(nan_error_check, prim)

add_nan_check(lax.floor_p)
add_nan_check(lax.ceil_p)
add_nan_check(lax.round_p)
add_nan_check(lax.sign_p)
add_nan_check(lax.shift_left_p)
add_nan_check(lax.shift_right_arithmetic_p)
add_nan_check(lax.shift_right_logical_p)
add_nan_check(lax.bitcast_convert_type_p)
add_nan_check(lax.real_p)
add_nan_check(lax.complex_p)
add_nan_check(lax.conj_p)
add_nan_check(lax.imag_p)
add_nan_check(lax.add_p)
add_nan_check(lax.sub_p)
add_nan_check(lax.convert_element_type_p)
add_nan_check(lax.broadcast_in_dim_p)
add_nan_check(lax.concatenate_p)
add_nan_check(lax.pad_p)
add_nan_check(lax.reshape_p)
add_nan_check(lax.rev_p)
add_nan_check(lax.transpose_p)
add_nan_check(lax.slice_p)
add_nan_check(lax.reduce_sum_p)
add_nan_check(lax.reduce_window_sum_p)
add_nan_check(lax.fft_p)
add_nan_check(lax.cumsum_p)
add_nan_check(lax.cumprod_p)
add_nan_check(lax.cummax_p)
add_nan_check(lax.cummin_p)
add_nan_check(lax.erf_p)
add_nan_check(lax.expm1_p)
add_nan_check(lax.log1p_p)
add_nan_check(lax.sqrt_p)
add_nan_check(lax.rsqrt_p)
add_nan_check(lax.asinh_p)
add_nan_check(lax.acosh_p)
add_nan_check(lax.atanh_p)
add_nan_check(lax.erfc_p)
add_nan_check(lax.rem_p)
add_nan_check(lax.clamp_p)
add_nan_check(lax.erf_inv_p)
add_nan_check(lax.exp_p)
add_nan_check(lax.pow_p)
add_nan_check(lax.integer_pow_p)
add_nan_check(lax.tanh_p)
add_nan_check(lax.log_p)
add_nan_check(lax.atan2_p)
add_nan_check(lax.sin_p)
add_nan_check(lax.cos_p)
add_nan_check(lax.sinh_p)
add_nan_check(lax.cosh_p)
add_nan_check(lax.dot_general_p)
add_nan_check(lax.mul_p)
add_nan_check(lax.conv_general_dilated_p)
add_nan_check(lax.reduce_max_p)
add_nan_check(lax.reduce_min_p)
add_nan_check(lax.abs_p)
add_nan_check(lax.select_p)
add_nan_check(lax.max_p)
add_nan_check(lax.min_p)


def assert_discharge_rule(error, enabled_errors, pred, code, *, msgs):
  if ErrorCategory.USER_CHECK not in enabled_errors:
    return [], error

  out_err = error.err | jnp.logical_not(pred)
  out_code = lax.select(error.err, error.code, code)
  return [], Error(out_err, out_code, {**error.msgs, **msgs})
error_checks[assert_p] = assert_discharge_rule


## checkify api

ErrorCategory = enum.Enum('ErrorCategory', ['NAN', 'OOB', 'DIV', 'USER_CHECK'])

float_errors = frozenset({ErrorCategory.NAN, ErrorCategory.DIV})
index_errors = frozenset({ErrorCategory.OOB})
automatic_errors = float_errors | index_errors
user_checks = frozenset({ErrorCategory.USER_CHECK})

Out = TypeVar('Out')


def checkify(fun: Callable[..., Out],
             errors: FrozenSet[ErrorCategory] = user_checks
             ) -> Callable[..., Tuple[Error, Out]]:
  """Functionalize `check` calls in `fun`, and optionally add run-time error checks.

  Run-time errors are either user-added ``checkify.check`` assertions, or
  automatically added checks like NaN checks, depending on the ``errors``
  argument.

  The returned function will return an Error object `err` along with the output
  of the original function. ``err.get()`` will either return ``None`` (if no
  error occurred) or a string containing an error message. This error message
  will correspond to the first error which occurred.

  The kinds of errors are:
    - ErrorCategory.USER_CHECK: a ``checkify.check`` predicate evaluated
      to False.
    - ErrorCategory.NAN: a floating-point operation generated a NaN value
      as output.
    - ErrorCategory.DIV: division by zero
    - ErrorCategory.OOB: an indexing operation was out-of-bounds

  Multiple categories can be enabled together by creating a `Set` (eg.
  ``errors={ErrorCategory.NAN, ErrorCategory.OOB}``).

  Args:
    fun: Callable which can contain user checks (see ``check``).
    errors: A set of ErrorCategory values which defines the set of enabled
      checks. By default only explicit ``check``s are enabled
      (``{ErrorCategory.USER_CHECK}``). You can also for example enable NAN and
      DIV errors through passing the ``checkify.float_errors`` set, or for
      example combine multiple sets through set operations
      (``checkify.float_errors|checkify.user_checks``)
  Returns:
    A function which accepts the same arguments as ``fun`` and returns as output
    a pair where the first element is an ``Error`` value, representing any
    failed ``check``s, and the second element is the original output of ``fun``.

  For example:

    >>> import jax
    >>> import jax.numpy as jnp
    >>> from jax.experimental import checkify
    >>>
    >>> @jax.jit
    ... def f(x):
    ...   y = jnp.sin(x)
    ...   return x+y
    >>> err, out = checkify.checkify(f, errors=checkify.float_errors)(jnp.inf)
    >>> err.throw()  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: nan generated by primitive sin

  """
  if not errors:
    raise ValueError('Checkify needs to be called with at least one enabled'
                     ' ErrorCategory, was called with an empty errors set.')

  @traceback_util.api_boundary
  def checked_fun(*args, **kwargs):
    args_flat, in_tree = tree_flatten((args, kwargs))
    f, out_tree = flatten_fun(lu.wrap_init(fun), in_tree)
    (err, code, out_flat), msgs = checkify_flat(f, errors, *args_flat)
    out = tree_unflatten(out_tree(), out_flat)
    return Error(err, code, msgs), out
  return checked_fun
