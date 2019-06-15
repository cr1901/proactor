#!/usr/bin/env python3

import sys, os, json, shutil

sys.path += ["C:\\msys64\\mingw64\\share\\yosys\\python3"]
from core_mcve import SbyJob, SbyAbort

with open("config.json", "rb") as fp:
    config = json.load(fp)

shutil.rmtree("demo3", ignore_errors=True)
shutil.copytree("demo3-hier", "demo3")
job = SbyJob(config, "demo3", "", False)

try:
    job.run(False)
except SbyAbort:
    pass

sys.exit(job.retcode)
