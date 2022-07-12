import torch
import triton
import triton.language as tl
import copy
import pytest
import ast
import itertools

torch.manual_seed(0)

# convert from string to torch.dtype
# Necessary because doesn't print torch.dtype properly
cvt = {
    'bool': torch.bool,
    'int8': torch.int8,
    'int16': torch.int16,
    'int32': torch.int32,
    'int64': torch.int64,
    'bfloat16': torch.bfloat16,
    'float16': torch.float16,
    'float32': torch.float32,
    'float64': torch.float64,
}

int_dtypes = ['int8', 'int16', 'int32', 'int64']
float_dtypes = ['float16', 'float32', 'float64']
dtypes = int_dtypes + float_dtypes


def patch_kernel(template, to_replace):
    kernel = copy.deepcopy(template)
    for key, value in to_replace.items():
        kernel.src = kernel.src.replace(key, value)
    return kernel


# generic test functions
def _test_unary(dtype_x, expr, torch_expr=None, device='cuda'):
    SIZE = 128
    # define the kernel / launch-grid
    @triton.jit
    def kernel(Z, X, **meta):
        off = tl.arange(0, meta['SIZE'])
        x = tl.load(X + off)
        z = GENERATE_TEST_HERE
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    x = triton.testing.random(SIZE, dtype=cvt[dtype_x], device=device)
    if 'log' in expr: x = torch.abs(x) + 0.01
    # reference result
    z_ref = eval(expr if torch_expr is None else torch_expr)
    # triton result
    z_tri = torch.empty_like(z_ref)
    kernel[(1, )](z_tri, x, SIZE=SIZE, num_warps=4)
    # compare
    triton.testing.assert_allclose(z_ref, z_tri)


def _test_binary(dtype_x, dtype_y, expr, device='cuda'):
    SIZE = 128
    # define the kernel / launch-grid
    @triton.jit
    def kernel(Z, X, Y, **meta):
        off = tl.arange(0, meta['SIZE'])
        x = tl.load(X + off)
        y = tl.load(Y + off)
        z = GENERATE_TEST_HERE
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    x = triton.testing.random(SIZE, dtype=cvt[dtype_x], device=device)
    y = triton.testing.random(SIZE, dtype=cvt[dtype_y], device=device)
    # reference result
    z_ref = eval(expr)
    # triton result
    z_tri = torch.empty(SIZE, dtype=z_ref.dtype, device=device)
    kernel[(1, )](z_tri, x, y, SIZE=SIZE, num_warps=4)
    # compare
    triton.testing.assert_allclose(z_ref, z_tri)


# ---------------
# test binary ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, expr", [
    (dtype_x, dtype_y, f' x {op} y') \
  for op in ['+', '-', '*', '/', '%'] \
  for dtype_x in dtypes \
  for dtype_y in dtypes
])
def test_bin_op(dtype_x, dtype_y, expr, device='cuda'):
    _test_binary(dtype_x, dtype_y, expr, device=device)


# ---------------
# test bitwise ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, expr", [
    (dtype_x, dtype_y, f' x {op} y') \
  for op in ['&', '|', '^'] \
  for dtype_x in dtypes \
  for dtype_y in dtypes
])
def test_bitwise_op(dtype_x, dtype_y, expr, device='cuda'):
    if 'float' in dtype_x + dtype_y:
        with pytest.raises(RuntimeError):
            _test_binary(dtype_x, dtype_y, expr, device=device)
    else:
        _test_binary(dtype_x, dtype_y, expr, device=device)


# ---------------
# test compare ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, expr", [
    (dtype_x, dtype_y, f' x {op} y') \
    for op in ['==', '!=', '>', '<', '>=', '<='] \
    for dtype_x in dtypes \
    for dtype_y in dtypes
])
def test_compare_op(dtype_x, dtype_y, expr, device='cuda'):
    _test_binary(dtype_x, dtype_y, expr, device=device)


# ---------------
# test unary ops
# ---------------
@pytest.mark.parametrize("dtype_x, expr", ([(dtype_x, ' -x') for dtype_x in float_dtypes] + [(dtype_x, ' ~x') for dtype_x in int_dtypes]))
def test_unary_op(dtype_x, expr, device='cuda'):
    _test_unary(dtype_x, expr, device=device)

# ----------------
# test math ops
# ----------------
# @pytest.mark.paramterize("expr", [
#     'exp', 'log', 'cos', 'sin'
# ])

@pytest.mark.parametrize("expr", [
    'exp', 'log', 'cos', 'sin'
])
def test_math_op(expr, device='cuda'):
    _test_unary('float32', f'tl.{expr}(x)', f'torch.{expr}(x) ', device=device)


# ----------------
# test indexing
# ----------------


def make_ptr_str(name, shape):
    rank = len(shape)
    offsets = []
    stride = 1
    for i in reversed(range(rank)):
        idx = ', '.join([':' if ii == i else 'None' for ii in range(rank)])
        offsets += [f'tl.arange(0, {shape[i]})[{idx}]*{stride}']
        stride *= shape[i]
    return f"{name} + {' + '.join(offsets)}"


@pytest.mark.parametrize("expr", [f'x[{s}]' for s in
    ['None, :', ':, None',\
     'None, :, :', ':, :, None']\
])
def test_index1d(expr, device='cuda'):
    dtype = torch.int32
    rank_x = expr.count(':')
    rank_y = expr.count(',') + 1
    shape_x = [32 for _ in range(rank_x)]
    shape_z = [32 for _ in range(rank_y)]

    # Triton kernel
    @triton.jit
    def kernel(Z, X, **meta):
        SIZE = meta['SIZE']
        m = tl.arange(0, SIZE)
        n = tl.arange(0, SIZE)
        x = tl.load(X_PTR_EXPR)
        z = GENERATE_TEST_HERE
        tl.store(Z_PTR_EXPR, z)

    to_replace = {
        'X_PTR_EXPR': make_ptr_str('X', shape_x),
        'Z_PTR_EXPR': make_ptr_str('Z', shape_z),
        'GENERATE_TEST_HERE': expr,
    }
    kernel = patch_kernel(kernel, to_replace)

    # torch result
    x = triton.testing.random(shape_x, dtype=dtype, device=device)
    y = torch.zeros(shape_z, dtype=dtype, device=device)
    z_ref = eval(expr) + y
    # triton result
    z_tri = torch.empty_like(z_ref)
    kernel[(1, )](z_tri, x, num_warps=1, SIZE=shape_x[0])
    # compare
    triton.testing.assert_allclose(z_ref, z_tri)


# ---------------
# test tuples
# ---------------


@triton.jit
def fn(a, b):
    return a + b, \
            a - b, \
            a * b


def test_tuples():
    device = 'cuda'

    @triton.jit
    def with_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = fn(x, y)
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    @triton.jit
    def without_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = x + y, x - y, x * y
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    x = torch.tensor([1.3], device=device, dtype=torch.float32)
    y = torch.tensor([1.9], device=device, dtype=torch.float32)
    a_tri = torch.tensor([0], device=device, dtype=torch.float32)
    b_tri = torch.tensor([0], device=device, dtype=torch.float32)
    c_tri = torch.tensor([0], device=device, dtype=torch.float32)
    for kernel in [with_fn, without_fn]:
        kernel[(1, )](x, y, a_tri, b_tri, c_tri, num_warps=1)
        a_ref, b_ref, c_ref = x + y, x - y, x * y
        assert a_tri == a_ref
        assert b_tri == b_ref
        assert c_tri == c_ref


# ---------------
# test atomics
# ---------------
@pytest.mark.parametrize("op, dtype_x, mode", itertools.chain.from_iterable([
    [('add', 'int32', mode), ('add', 'float16', mode), ('add', 'float32', mode), \
    ('max', 'int32', mode), ('max', 'float32', mode),\
    ('min', 'int32', mode), ('min', 'float32', mode),\
    ]
    for mode in ['all_neg', 'all_pos', 'min_neg', 'max_pos']]))
def test_atomic_rmw(op, dtype_x, mode, device='cuda'):
    dtype_x = cvt[dtype_x]
    n_programs = 37

    # triton kernel
    @triton.jit
    def kernel(X, Z, **meta):
        pid = tl.program_id(0)
        x = tl.load(X + pid)
        old = GENERATE_TEST_HERE

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': f'tl.atomic_{op}(Z, x)'})
    torch_op = {'add': torch.sum, 'max': torch.max, 'min': torch.min}[op]
    max_neutral = float('-inf') if dtype_x.is_floating_point else torch.iinfo(dtype_x).min
    min_neutral = float('inf') if dtype_x.is_floating_point else torch.iinfo(dtype_x).max
    neutral = {'add': 0, 'max': max_neutral, 'min': min_neutral}[op]

    # triton result
    x_tri = triton.testing.random((n_programs, ), dtype=dtype_x, device=device)
    if mode == 'all_neg':
        x_tri = -torch.abs(x_tri)
    if mode == 'all_pos':
        x_tri = torch.abs(x_tri)
    if mode == 'min_neg':
        idx = torch.randint(n_programs, size=(1, )).item()
        x_tri[idx] = -torch.max(torch.abs(x_tri)) - 1
    if mode == 'max_pos':
        idx = torch.randint(n_programs, size=(1, )).item()
        x_tri[idx] = torch.max(torch.abs(x_tri)) + 1

    z_tri = torch.empty([], dtype=dtype_x, device=device)
    z_tri.fill_(neutral)
    kernel[(n_programs, )](x_tri, z_tri)
    # torch result
    z_ref = torch_op(x_tri).to(dtype_x)
    # compare
    exact = op not in ['add']
    if exact:
        assert z_ref.item() == z_tri.item()
    else:
        triton.testing.assert_allclose(z_ref, z_tri)


# ---------------
# test cast
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_z, bitcast", [
    (dtype_x, dtype_z, False) \
                        for dtype_x in dtypes\
                        for dtype_z in dtypes
] + [ 
    ('float32', 'bfloat16', False),
    ('bfloat16', 'float32', False),
    ('float32', 'int32', True)
])
def test_cast(dtype_x, dtype_z, bitcast, device='cuda'):
    x = torch.tensor([43.5], dtype=cvt[dtype_x], device=device)

    # triton kernel
    @triton.jit
    def kernel(X, Z, **meta):
        x = tl.load(X)
        z = x.to(Z.dtype.element_ty, bitcast=meta['BITCAST'])
        tl.store(Z, z)

    # triton result
    z_tri = torch.empty((1, ), dtype=cvt[dtype_z], device=device)
    kernel[(1, )](x, z_tri, BITCAST=bitcast)
    # torch result
    if bitcast:
        import numpy as np
        z_ref = x.detach().cpu().numpy().view(getattr(np, dtype_z))
        z_ref = torch.from_numpy(z_ref).to(device)
    else:
        z_ref = x.to(z_tri.dtype)
    assert z_tri == z_ref


# ---------------
# test load
# ---------------

# ---------------
# test store
# ---------------

# ---------------
# test if
# ---------------

# ---------------
# test for
# ---------------

# ---------------
# test while
# ---------------
