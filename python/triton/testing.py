import torch
import os
from .code_gen import OutOfResources

try:
    import triton._C.libtriton.cutlass as _cutlass
    has_cutlass = True
except ImportError:
    _cutlass = None
    has_cutlass = False

def catch_oor(kernel, pytest_handle=None):
    try:
        res = kernel()
    except OutOfResources as e:
        if pytest_handle:
            pytest_handle.skip(str(e))
        return None
    return res


def sparsify_tensor(x, mask, block):
    ret = torch.empty((x.size(0), mask.sum(), block, block), dtype=x.dtype, device=x.device)
    for idx, (h, i, j) in enumerate(zip(*mask.nonzero(as_tuple=True))):
        ret[:, idx, :, :] = x[:, h, i * block:(i + 1) * block, j * block:(j + 1) * block]
    return ret


def cutlass_matmul(a, b):
    if _cutlass is None:
        raise RuntimeError("Cannot find cutlass library")
    M, N = a.shape[0], b.shape[1]
    Ka, Kb = a.shape[1], b.shape[0]
    assert Ka == Kb
    assert a.dtype == b.dtype
    assert a.device == b.device
    # allocate output
    c = torch.empty_strided((M, N), (1, M), dtype=a.dtype, device=a.device)
    # run function
    dtype = str(a.dtype).split('.')[-1]
    _cutlass.matmul(a.data_ptr(), b.data_ptr(), c.data_ptr(), \
                    M, N, Ka,\
                    a.stride(0), a.stride(1),\
                    b.stride(0), b.stride(1),\
                    c.stride(0), c.stride(1),\
                    dtype, dtype, dtype,
                    a.device.index, torch.cuda.current_stream(a.device).cuda_stream)

    return c


def mask_tensor(x, mask, block, value=0):
    ret = x.clone()
    for h, i, j in zip(*(mask == 0).nonzero(as_tuple=True)):
        ret[:, h, i * block:(i + 1) * block, j * block:(j + 1) * block] = value
    return ret


def allclose(x, y, tol=1e-2):
    if x.dtype != y.dtype:
        raise RuntimeError(f'{x.dtype} did not match with {x.dtype}')
    if x.shape != y.shape:
        raise RuntimeError(f'{x.shape} did not match with {y.shape}')
    if x.dtype == torch.bool:
        return torch.sum(x ^ y) == 0
    if x.dtype in [torch.int8, torch.int16, torch.int32, torch.int64]:
        tol = 0
    diff = abs(x - y)
    x_max = torch.max(x)
    y_max = torch.max(y)
    tol = 1e-2
    err = torch.max(diff) / torch.max(x_max, y_max)
    return err <= tol


def assert_allclose(x, y, tol=1e-2):
    assert x.dtype == y.dtype
    assert allclose(x, y, tol)


def random(shape, dtype, device):
    if isinstance(shape, int):
        shape = (shape, )
    if dtype == torch.bool:
        return torch.randint(0, 2, shape, dtype=dtype, device=device)
    if dtype in [torch.int8, torch.int16, torch.int32, torch.int64]:
        return torch.randint(1, 32, shape, dtype=dtype, device=device)
    if dtype in [torch.float16, torch.float32, torch.float64]:
        return torch.normal(0, 10, shape, dtype=dtype, device=device)
    raise RuntimeError(f'Unknown dtype {dtype}')


def do_bench(fn, warmup=25, rep=100, grad_to_none=None, percentiles=[0.2, 0.8]):
    """
    Benchmark the runtime of the provided function. By default, return the median runtime of :code:`fn` along with
    the 20-th and 80-th performance percentile.

    :param fn: Function to benchmark
    :type fn: Callable
    :param warmup: Warmup time (in ms)
    :type warmup: int
    :param rep: Repetition time (in ms)
    :type rep: int
    :param grad_to_none: Reset the gradient of the provided tensor to None
    :type grad_to_none: torch.tensor, optional
    :param percentiles: Performance percentile to return in addition to the median.
    :type percentiles: list[float]
    """

    # Estimate the runtime of the function
    fn()
    torch.cuda.synchronize()
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    start_event.record()
    for _ in range(5):
        fn()
    end_event.record()
    torch.cuda.synchronize()
    estimate_ms = start_event.elapsed_time(end_event) / 5
    # We maintain a buffer of 256 MB that we clear
    # before each kernel call to make sure that the L2
    # doesn't contain any input data before the run
    start_event = [torch.cuda.Event(enable_timing=True) for _ in range(rep)]
    end_event = [torch.cuda.Event(enable_timing=True) for _ in range(rep)]
    cache = torch.empty(int(256e6), dtype=torch.int8, device='cuda')
    # Warm-up
    for _ in range(int(warmup / estimate_ms)):
        fn()
    # Benchmark
    for i in range(rep):
        # we don't want `fn` to accumulate gradient values
        # if it contains a backward pass. So we clear the
        # provided gradients
        if grad_to_none is not None:
            for x in grad_to_none:
                x.grad = None
        # we clear the L2 cache before each run
        cache.zero_()
        # record time of `fn`
        start_event[i].record()
        fn()
        end_event[i].record()
    torch.cuda.synchronize()
    times = torch.tensor([s.elapsed_time(e) for s, e in zip(start_event, end_event)])
    percentiles = torch.quantile(times, torch.tensor(percentiles)).tolist()
    med_ms = torch.median(times).item()
    return tuple([med_ms] + percentiles) if percentiles else med_ms


class Benchmark:
    """
    This class is used by the :code:`perf_report` function to generate line plots with a concise API.
    """
    def __init__(
        self,
        x_names,
        x_vals,
        line_arg,
        line_vals,
        line_names,
        plot_name,
        args,
        xlabel='',
        ylabel='',
        x_log=False,
        y_log=False,
        color=None,
        styles=None,
    ):
        """
        Constructor 

        :param x_names: Name of the arguments that should appear on the x axis of the plot. If the list contains more than one element, all the arguments are assumed to have the same value.
        :type x_names: List[str]
        :param x_vals: List of values to use for the arguments in :code:`x_names`.
        :type x_vals: List[Any]
        :param line_arg: Argument name for which different values correspond to different lines in the plot.
        :type line_arg: str
        :param line_vals: List of values to use for the arguments in :code:`line_arg`.
        :type line_vals: List[str]
        :param line_names: Label names for the different lines.
        :type line_names: List[str]
        :param plot_name: Name of the plot.
        :type plot_name: str
        :param args: List of arguments to remain fixed throughout the benchmark.
        :type args: List[str]
        :param xlabel: Label for the x axis of the plot.
        :type xlabel: str, optional
        :param ylabel: Label for the y axis of the plot.
        :type ylabel: str, optional
        :param x_log: Whether the x axis should be log scale.
        :type x_log: bool, optional
        :param y_log: Whether the y axis should be log scale.
        :type y_log: bool, optional
        """
        self.x_names = x_names
        self.x_vals = x_vals
        self.x_log = x_log
        self.line_arg = line_arg
        self.line_vals = line_vals
        self.line_names = line_names
        self.y_log = y_log
        self.styles = styles
        # plot info
        self.xlabel = xlabel
        self.ylabel = ylabel
        self.plot_name = plot_name
        self.args = args


class Mark:
    def __init__(self, fn, benchmarks):
        self.fn = fn
        self.benchmarks = benchmarks

    def _run(self, bench, save_path, show_plots, print_data):
        import matplotlib.pyplot as plt
        import pandas as pd
        import os
        y_mean = bench.line_names
        y_min = [f'{x}-min' for x in bench.line_names]
        y_max = [f'{x}-max' for x in bench.line_names]
        df = pd.DataFrame(columns=[bench.x_names[0]] + y_mean + y_min + y_max)
        for x in bench.x_vals:
            x_args = {x_name: x for x_name in bench.x_names}
            row_mean, row_min, row_max = [], [], []
            for y in bench.line_vals:
                ret = self.fn(**x_args, **{bench.line_arg: y}, **bench.args)
                try:
                    y_mean, y_min, y_max = ret
                except TypeError:
                    y_mean, y_min, y_max = ret, None, None
                row_mean += [y_mean]
                row_min += [y_min]
                row_max += [y_max]
            df.loc[len(df)] = [x] + row_mean + row_min + row_max
        if bench.plot_name:
            plt.figure()
            ax = plt.subplot()
            x = bench.x_names[0]
            for i, y in enumerate(bench.line_names):
                y_min, y_max = df[f'{y}-min'], df[f'{y}-max']
                col = bench.styles[i][0] if bench.styles else None
                sty = bench.styles[i][1] if bench.styles else None
                ax.plot(df[x], df[y], label=y, color=col, ls=sty)
                if y_min is not None and y_max is not None:
                    ax.fill_between(df[x], y_min, y_max, alpha=0.15, color=col)
            ax.legend()
            xlabel = bench.xlabel or " = ".join(bench.x_names)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(bench.ylabel)
            #ax.set_title(bench.plot_name)
            ax.set_xscale("log" if bench.x_log else "linear")
            ax.set_yscale("log" if bench.y_log else "linear")
            if show_plots:
                plt.show()
            if save_path:
                plt.savefig(os.path.join(save_path, f"{bench.plot_name}.png"))
        df = df[[bench.x_names[0]] + bench.line_names]
        if print_data:
            print(f'{bench.plot_name}:')
            print(df)
        if save_path:
            df.to_csv(os.path.join(save_path, f"{bench.plot_name}.csv"), float_format='%.1f', index=False)

    def run(self, show_plots=False, print_data=False, save_path=''):
        has_single_bench = isinstance(self.benchmarks, Benchmark)
        benchmarks = [self.benchmarks] if has_single_bench else self.benchmarks
        if save_path:
            html = open(os.path.join(save_path, "results.html"), "w")
            html.write("<html><body>\n")
        for bench in benchmarks:
            self._run(bench, save_path, show_plots, print_data)
            if save_path:
                html.write(f"<image src=\"{bench.plot_name}.png\"/>\n")
        if save_path:
            html.write("</body></html>\n")


def perf_report(benchmarks):
    """
    Mark a function for benchmarking. The benchmark can then be executed by using the :code:`.run` method on the return value.

    :param benchmarks: Benchmarking configurations.
    :type benchmarks: List of :class:`Benchmark`
    """
    return lambda fn: Mark(fn, benchmarks)
