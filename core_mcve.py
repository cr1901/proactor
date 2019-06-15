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
if os.name == "posix":
    import resource
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

if os.name == "posix":
    signal.signal(signal.SIGHUP, force_shutdown)
signal.signal(signal.SIGINT, force_shutdown)
signal.signal(signal.SIGTERM, force_shutdown)

class SbyTask:
    def __init__(self, job, info, deps, cmdline, logfile=None, logstderr=True):
        self.running = False
        self.finished = False
        self.terminated = False
        self.checkretcode = False
        self.job = job
        self.info = info
        self.deps = deps
        if os.name == "posix":
            self.cmdline = cmdline
        else:
            # Windows command interpreter equivalents for sequential
            # commands (; => &) command grouping ({} => ()).
            replacements = {
                ";" : "&",
                "{" : "(",
                "}" : ")",
            }

            cmdline_copy = cmdline
            for u, w in replacements.items():
                cmdline_copy = cmdline_copy.replace(u, w)
            self.cmdline = cmdline_copy
        self.logfile = logfile
        self.noprintregex = None
        self.notify = []
        self.linebuffer = ""
        self.logstderr = logstderr

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
            if self.logfile is not None:
                print(line, file=self.logfile)
            self.job.log("%s: %s" % (self.info, line))

    def handle_exit(self, retcode):
        if self.terminated:
            return
        if self.logfile is not None:
            self.logfile.close()
        if self.exit_callback is not None:
            self.exit_callback(retcode)

    def terminate(self, timeout=False):
        if self.job.opt_wait and not timeout:
            return
        if self.running:
            self.job.log("%s: terminating process" % self.info)
            if os.name != "posix":
                # self.p.terminate does not actually terminate underlying
                # processes on Windows, so use taskkill to kill the shell
                # and children. This for some reason does not cause the
                # associated future (self.fut) to complete until it is awaited
                # on one last time.
                # subprocess.Popen("taskkill /T /F /PID {}".format(self.p.pid), stdin=subprocess.DEVNULL,
                #     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.p.terminate()
            else:
                os.killpg(self.p.pid, signal.SIGTERM)
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

            self.job.log("%s: starting process \"%s\"" % (self.info, self.cmdline))
            if os.name == "posix":
                def preexec_fn():
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                    os.setpgrp()

                subp_kwargs = { "preexec_fn" : preexec_fn }
            else:
                subp_kwargs = { "creationflags" : subprocess.CREATE_NEW_PROCESS_GROUP }

            self.p = await asyncio.create_subprocess_shell(self.cmdline, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=(asyncio.subprocess.STDOUT if self.logstderr else None),
                    **subp_kwargs)
            self.job.tasks_pending.remove(self)
            self.job.tasks_running.append(self)
            all_tasks_running.append(self)
            self.running = True
            asyncio.ensure_future(self.output())
            self.fut = asyncio.ensure_future(self.p.wait())

    async def shutdown_and_notify(self):
        self.job.log("%s: finished (returncode=%d)" % (self.info, self.p.returncode))
        self.job.tasks_running.remove(self)
        self.job.tasks_retired.append(self)
        self.running = False

        self.handle_exit(self.p.returncode)

        if self.checkretcode and self.p.returncode != 0:
            self.job.status = "ERROR"
            self.job.log("%s: job failed. ERROR." % self.info)
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

        if os.name == "posix":
            ru = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.start_process_time = ru.ru_utime + ru.ru_stime

        self.summary = list()

        self.logfile = open("%s/logfile.txt" % workdir, "a")

    def taskloop(self):
        if os.name != "posix":
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)
        loop = asyncio.get_event_loop()
        poll_fut = asyncio.ensure_future(self.task_poller())
        loop.run_until_complete(poll_fut)

    async def timekeeper(self):
        total_clock_time = int(time() - self.start_clock_time)

        try:
            while total_clock_time <= self.opt_timeout:
                await asyncio.sleep(1)
                total_clock_time = int(time() - self.start_clock_time)
        except asyncio.CancelledError:
            pass

    def timeout(self, fut):
        self.log("Reached TIMEOUT (%d seconds). Terminating all tasks." % self.opt_timeout)
        self.status = "TIMEOUT"
        self.terminate(timeout=True)

    async def task_poller(self):
        if self.opt_timeout is not None:
            timer_fut = asyncio.ensure_future(self.timekeeper())
            done_cb = partial(SbyJob.timeout, self)
            timer_fut.add_done_callback(done_cb)

        for task in self.tasks_pending:
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

        if self.opt_timeout is not None:
            timer_fut.remove_done_callback(done_cb)
            timer_fut.cancel()

        # Required on Windows. I am unsure why, but subprocesses that were
        # terminated will not have their futures complete until awaited on
        # one last time.
        if os.name != "posix":
            for t in self.tasks_retired:
                if not t.fut.done():
                    await t.fut

    def log(self, logmessage):
        tm = localtime()
        print("SBY %2d:%02d:%02d [%s] %s" % (tm.tm_hour, tm.tm_min, tm.tm_sec, self.workdir, logmessage), flush=True)
        print("SBY %2d:%02d:%02d [%s] %s" % (tm.tm_hour, tm.tm_min, tm.tm_sec, self.workdir, logmessage), file=self.logfile, flush=True)

    def error(self, logmessage):
        raise SbyAbort(logmessage)

    def make_model(self, model_name):
        if model_name in ["base", "nomem"]:
            task = SbyTask(self, model_name, [],
                    "cd %s/src; %s -ql ../model/design%s.log ../model/design%s.ys" % (self.workdir, "yosys",
                    "" if model_name == "base" else "_nomem", "" if model_name == "base" else "_nomem"))
            task.checkretcode = True

            return [task]

        if re.match(r"^smt2(_syn)?(_nomem)?(_stbv|_stdt)?$", model_name):
            task = SbyTask(self, model_name, self.model("nomem" if "_nomem" in model_name else "base"),
                    "cd %s/model; %s -ql design_%s.log design_%s.ys" % (self.workdir, "yosys", model_name, model_name))
            task.checkretcode = True

            return [task]

        assert False

    def model(self, model_name):
        if model_name not in self.models:
            self.models[model_name] = self.make_model(model_name)
        return self.models[model_name]

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

        self.__dict__["opt_wait"] = False
        self.__dict__["opt_timeout"] = None

        

        for engine_idx in range(len(self.engines)):
            engine = self.engines[engine_idx]
            assert len(engine) > 0

            self.log("engine_%d: %s" % (engine_idx, " ".join(engine)))
            os.makedirs("%s/engine_%d" % (self.workdir, engine_idx))

            bin_name = "yices" if engine_idx == 0 else "z3"
            task = SbyTask(self, "engine_%d" % engine_idx, self.model("smt2"),
                      "cd demo3& yosys-smtbmc -s %s --presat --unroll --noprogress -t 30 --append 0 --dump-vcd engine_%d/trace.vcd --dump-vlogtb engine_%d/trace_tb.v --dump-smtc engine_%d/trace.smtc model/design_smt2.smt2" %
                            (bin_name, engine_idx, engine_idx, engine_idx),
                      logfile=open("demo3/engine_0/logfile.txt", "w"), logstderr=True)

            task_status = None

            def output_callback(line):
                nonlocal task_status

                match = re.match(r"^## [0-9: ]+ Status: FAILED", line)
                if match: task_status = "FAIL"

                match = re.match(r"^## [0-9: ]+ Status: PASSED", line)
                if match: task_status = "PASS"

                return line

            def exit_callback(retcode):
                self.update_status(task_status)
                self.log("engine_%d: Status returned by engine: %s" % (engine_idx, task_status))
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

    # Fake creating the directory hierarchy
    shutil.rmtree("demo3", ignore_errors=True)
    with zipfile.ZipFile("demo3.zip", "r") as zf:
        zf.extractall()

    # Config is dummied out/provided by demo3.
    job = SbyJob([], "demo3", "", False)

    try:
        job.run(False)
    except SbyAbort:
        pass

    sys.exit(job.retcode)
