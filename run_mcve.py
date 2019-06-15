#!/usr/bin/env python3

import sys, os, json, shutil

sys.path += ["C:\\msys64\\mingw64\\share\\yosys\\python3"]
from core_mcve import SbyJob, SbyAbort

# Fake creating the directory hierarchy
shutil.rmtree("demo3", ignore_errors=True)
shutil.copytree("demo3-hier", "demo3")

# Config is dummied out/provided by demo3-hier.
job = SbyJob([], "demo3", "", False)

try:
    job.run(False)
except SbyAbort:
    pass

sys.exit(job.retcode)
