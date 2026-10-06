"""Writes docs/COST.md.  Usage: python3 docs/make_cost.py [monthly hosting price in INR]"""
import os
import sys

price = float(sys.argv[1]) if len(sys.argv) > 1 else None
hw = ""
if os.path.exists("loadtest/results/hw.txt"):
    parts = open("loadtest/results/hw.txt").read().strip().split("\n")
    if parts and len(parts[0].split()) == 2:
        ncpu, mem = parts[0].split()
        hw = "%s CPUs and %.1f GB RAM available to Docker" % (ncpu, int(mem) / 2 ** 30)

lines = [
    "# Cost sheet",
    "",
    "## What costs money",
    "- One server that runs the whole stack (API, worker, scheduler, PostgreSQL, Redis). This is how it was tested.",
    "- A domain name and backup storage, if wanted.",
    "",
    "## What does not",
    "There are no per-use fees. The system calls no paid APIs: QR codes, PDF certificates and the check-in logic all run locally. Cost does not grow with the number of check-ins, only with the size of the server.",
    "",
    "## Evidence for sizing",
    "The 500-student check-in burst was measured on a laptop with %s, with the load generator on the same machine (see TEST_REPORT.md). It has not been measured on a cloud server, so a real deployment should be re-tested." % (hw or "unknown resources"),
    "",
    "## Cost per student",
    "cost per student per month = monthly server price / number of students",
    "",
]
if price is None:
    lines += ["The monthly hosting price has NOT been filled in. Run: python3 docs/make_cost.py <price in INR>"]
else:
    lines += ["| Students | Monthly server price (INR) | Cost per student per month (INR) |", "|---|---|---|"]
    for n in (350, 1000):
        lines.append("| %d | %.0f | %.2f |" % (n, price, price / n))
open("docs/COST.md", "w").write("\n".join(lines) + "\n")
print("docs/COST.md written" + (" (price not filled in)" if price is None else ""))
