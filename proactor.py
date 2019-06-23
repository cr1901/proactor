import os
import asyncio
import subprocess
import sys


async def spawn(infinite=False):
    echo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "bin", "echo.exe")
    args = "-i" if infinite else "-n 4"

    p = await asyncio.create_subprocess_shell("%s %s" % (echo_path, args),
                                              stdin=asyncio.subprocess.DEVNULL,
                                              stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.STDOUT)

    async def output():
        linebuffer = ""
        while True:
            outs = await p.stdout.readline()
            await asyncio.sleep(0)  # https://bugs.python.org/issue24532
            outs = outs.decode("utf-8")
            if len(outs) == 0:
                break
            if outs[-1] != '\n':
                linebuffer += outs
                break
            outs = (linebuffer + outs).strip()
            linebuffer = ""
            if outs is not None:
                print(outs)

    asyncio.ensure_future(output())
    return (asyncio.ensure_future(p.wait()), p)


async def task_poller(workaround):
    (short_fut, _) = await spawn()
    (long_fut, long_p) = await spawn(True)

    (_, _) = await asyncio.wait([short_fut, long_fut],
                                return_when=asyncio.FIRST_COMPLETED)
    if not workaround:
        long_p.terminate()  # echo -i never terminates without user
        # intervention, so process will still be alive.
    else:
        # Workaround that allows long_p future to complete.
        subprocess.Popen("taskkill /T /F /PID {}"
                         .format(long_p.pid), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Required for the workaround to actually work.
        await long_fut
    print("Task poller finished.")


if __name__ == "__main__":
    workaround = "-w" in sys.argv

    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    loop = asyncio.get_event_loop()
    poll_fut = asyncio.ensure_future(task_poller(workaround))
    loop.run_until_complete(poll_fut)
