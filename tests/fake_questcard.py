#!/usr/bin/env python3
import os, shutil, sys, time, uuid
d = os.path.join(os.path.dirname(os.path.realpath(__file__)), "runs", "shown")
os.makedirs(d, exist_ok=True)
shutil.copy(sys.argv[1], os.path.join(d, "%s-%s.json" % (int(time.time() * 1000), uuid.uuid4().hex[:6])))
