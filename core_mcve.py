#
# SymbiYosys (sby) -- Front-end for Yosys-based formal verification flows
#
# Copyright (C) 2016  Clifford Wolf <clifford@clifford.at>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#

import os, re, sys
import signal
import subprocess
import asyncio
from functools import partial
from time import time, localtime

all_tasks_running = []

def force_shutdown(signum, frame):
    print("SBY ---- Keyboard interrupt or external termination signal ----", flush=True)
    for task in list(all_tasks_running):
        task.terminate()
    sys.exit(1)

signal.signal(signal.SIGINT, force_shutdown)
signal.signal(signal.SIGTERM, force_shutdown)

class SbyTask:
    def __init__(self, job, info, deps, cmdline):
        self.running = False
        self.finished = False
        self.terminated = False
        self.checkretcode = False
        self.job = job
        self.info = info
        self.deps = deps

        self.cmdline = cmdline
        self.noprintregex = None
        self.notify = []
        self.linebuffer = ""

        self.job.tasks_pending.append(self)

        for dep in self.deps:
            if not dep.finished:
                dep.notify.append(self)

        self.output_callback = None
        self.exit_callback = None

    def handle_output(self, line):
        if self.terminated or len(line) == 0:
            return
        if self.output_callback is not None:
            line = self.output_callback(line)
        if line is not None and (self.noprintregex is None or not self.noprintregex.match(line)):
            print("%s: %s" % (self.info, line))

    def handle_exit(self, retcode):
        if self.terminated:
            return
        if self.exit_callback is not None:
            self.exit_callback(retcode)

    def terminate(self, timeout=False):
        if self.running:
            print("%s: terminating process" % self.info)
            # self.p.terminate does not actually terminate underlying
            # processes on Windows, so use taskkill to kill the shell
            # and children. This for some reason does not cause the
            # associated future (self.fut) to complete until it is awaited
            # on one last time.
            # subprocess.Popen("taskkill /T /F /PID {}".format(self.p.pid), stdin=subprocess.DEVNULL,
            #     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.p.terminate()
            self.job.tasks_running.remove(self)
            self.job.tasks_retired.append(self)
            all_tasks_running.remove(self)
        self.terminated = True

    async def output(self):
        while True:
            outs = await self.p.stdout.readline()
            await asyncio.sleep(0) # https://bugs.python.org/issue24532
            outs = outs.decode("utf-8")
            if len(outs) == 0: break
            if outs[-1] != '\n':
                self.linebuffer += outs
                break
            outs = (self.linebuffer + outs).strip()
            self.linebuffer = ""
            self.handle_output(outs)

    async def maybe_spawn(self):
        if self.finished or self.terminated:
            return

        if not self.running:
            for dep in self.deps:
                if not dep.finished:
                    return

            print("%s: starting process \"%s\"" % (self.info, self.cmdline))
            subp_kwargs = { "creationflags" : subprocess.CREATE_NEW_PROCESS_GROUP }

            self.p = await asyncio.create_subprocess_shell(self.cmdline, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **subp_kwargs)
            self.job.tasks_pending.remove(self)
            self.job.tasks_running.append(self)
            all_tasks_running.append(self)
            self.running = True
            asyncio.ensure_future(self.output())
            self.fut = asyncio.ensure_future(self.p.wait())

    async def shutdown_and_notify(self):
        print("%s: finished (returncode=%d)" % (self.info, self.p.returncode))
        self.job.tasks_running.remove(self)
        self.job.tasks_retired.append(self)
        self.running = False

        self.handle_exit(self.p.returncode)

        if self.checkretcode and self.p.returncode != 0:
            self.job.status = "ERROR"
            print("%s: job failed. ERROR." % self.info)
            self.terminated = True
            self.job.terminate()
            return

        self.finished = True
        for next_task in self.notify:
            await next_task.maybe_spawn()
        return

class SbyAbort(BaseException):
    pass


class SbyJob:
    def __init__(self, sbyconfig, workdir, early_logs, reusedir):
        self.engines = list()
        self.models = dict()
        self.workdir = workdir
        self.status = "UNKNOWN"
        self.expect = []

        self.tasks_running = []
        self.tasks_pending = []
        self.tasks_retired = []

        self.start_clock_time = time()

        self.summary = list()

    def taskloop(self):
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        loop = asyncio.get_event_loop()
        poll_fut = asyncio.ensure_future(self.task_poller())
        loop.run_until_complete(poll_fut)

    async def task_poller(self):
        # Make a copy b/c tasks_pending is modified by maybe_spawn.
        for task in list(self.tasks_pending):
            await task.maybe_spawn()

        while len(self.tasks_running):
            task_futs = []
            for task in self.tasks_running:
                if task.running:
                    task_futs.append(task.fut)
            (done, pending) = await asyncio.wait(task_futs, return_when=asyncio.FIRST_COMPLETED)

            for task in self.tasks_running:
                if task.fut in done:
                    await task.shutdown_and_notify()

        # Required on Windows. I am unsure why, but subprocesses that were
        # terminated will not have their futures complete until awaited on
        # one last time.
        for t in self.tasks_retired:
            if not t.fut.done():
                await t.fut

    def error(self, logmessage):
        raise SbyAbort(logmessage)

    def terminate(self, timeout=False):
        for task in list(self.tasks_running):
            task.terminate(timeout=timeout)

    def update_status(self, new_status):
        assert new_status in ["PASS", "FAIL", "UNKNOWN", "ERROR"]

        if new_status == "PASS":
            assert self.status != "FAIL"
            self.status = "PASS"

        else:
            assert 0

    def run(self, setupmode):
        mode = None
        key = None

        self.engines = [["smtbmc", "yices"], ["smtbmc", "z3"]]
        self.__dict__["opt_mode"] = "bmc"

        self.expect = ["PASS"]

        self.__dict__["opt_timeout"] = None


        for engine_idx in range(len(self.engines)):
            engine = self.engines[engine_idx]
            assert len(engine) > 0

            print("engine_%d: %s" % (engine_idx, " ".join(engine)))

            echo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo", "target", "debug", "echo.exe")
            args = "-i" if engine_idx == 0 else "-n 4"
            task = SbyTask(self, "engine_%d" % engine_idx, [], "%s %s" % (echo_path, args))

            task_status = None

            def output_callback(line):
                nonlocal task_status

                task_status = "PASS"

                return line

            def exit_callback(retcode):
                self.update_status(task_status)
                print("engine_%d: Status returned by engine: %s" % (engine_idx, task_status))
                self.summary.append("engine_%d (%s) returned %s" % (engine_idx, " ".join(engine), task_status))

                self.terminate()

            task.output_callback = output_callback
            task.exit_callback = exit_callback

        self.taskloop()

        if self.status in self.expect:
            self.retcode = 0
        else:
            assert False


if __name__ == "__main__":
    import sys, os, shutil, zipfile

    sys.path += ["C:\\msys64\\mingw64\\share\\yosys\\python3"]

    # Config is dummied out/provided by demo3.
    job = SbyJob([], "demo3", "", False)

    try:
        job.run(False)
    except SbyAbort:
        pass

    sys.exit(job.retcode)
