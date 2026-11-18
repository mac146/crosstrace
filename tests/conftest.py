"""Test collection rules.

The eBPF research code (collectors, faultlab, eval) calls Linux-only functions at import time, so these modules cannot even be imported on
Windows or macOS. The product (crosstrace/) runs everywhere and all of its tests are collected on every platform.
"""
import sys

collect_ignore = [] if sys.platform.startswith("linux") else ["test_obi_vs_join.py", "test_real_path.py"]
