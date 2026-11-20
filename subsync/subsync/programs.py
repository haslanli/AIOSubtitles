"""The programs the sync starts on data that comes from files anyone can publish: ffmpeg's audio decoders and alass
. On Linux each one runs under limits of its own, on its address space and its processor time, which the shell
sets just before the program starts (no preexec_fn: the worker has threads); past them the program dies, and its caller
takes that as the program's failure.

Standard library only."""
import sys


def limited(cmd, name, memory_kb, cpu_s, linux=None):
    """`cmd` (a program and its arguments) to run with at most `memory_kb` KiB of address space and `cpu_s` seconds of
    processor time on Linux (`linux`: whether to, for tests), as is elsewhere. The limits are set where the shell can
    set them: a shell that can't still runs the program. `name`: the program's name for the shell ($0)."""
    on_linux = sys.platform.startswith("linux") if linux is None else linux
    if not on_linux:
        return list(cmd)
    return ["sh", "-c", f'ulimit -v {memory_kb} 2>/dev/null; ulimit -t {cpu_s} 2>/dev/null; exec "$@"', name, *cmd]
