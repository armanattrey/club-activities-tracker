# Cost sheet

## What costs money
- One server that runs the whole stack (API, worker, scheduler, PostgreSQL, Redis). This is how it was tested.
- A domain name and backup storage, if wanted.

## What does not
There are no per-use fees. The system calls no paid APIs: QR codes, PDF certificates and the check-in logic all run locally. Cost does not grow with the number of check-ins, only with the size of the server.

## Evidence for sizing
The 500-student check-in burst was measured on a laptop with 10 CPUs and 7.7 GB RAM available to Docker, with the load generator on the same machine (see TEST_REPORT.md). It has not been measured on a cloud server, so a real deployment should be re-tested.

## Cost per student
cost per student per month = monthly server price / number of students

The monthly hosting price has NOT been filled in. Run: python3 docs/make_cost.py <price in INR>
