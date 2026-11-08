# Build the compiled collector into ~/.crosstrace-build (user-writable; the sudoers rule names that path).
# Needs: clang, bpftool, libbpf headers (libbpf-dev), kernel BTF at /sys/kernel/btf/vmlinux.
OUT ?= $(HOME)/.crosstrace-build
ARCH := $(shell uname -m | sed 's/x86_64/x86/; s/aarch64/arm64/')

all: $(OUT)/ctrace

$(OUT):
	mkdir -p $(OUT)

$(OUT)/vmlinux.h: | $(OUT)
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > $@

$(OUT)/ctrace.bpf.o: ctrace.bpf.c ctrace.h $(OUT)/vmlinux.h
	clang -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) -I$(OUT) -I. -c ctrace.bpf.c -o $@

$(OUT)/ctrace.skel.h: $(OUT)/ctrace.bpf.o
	bpftool gen skeleton $< > $@

$(OUT)/ctrace: ctrace.c ctrace.h $(OUT)/ctrace.skel.h
	clang -O2 -I$(OUT) -I. ctrace.c -o $@ -lbpf -lelf -lz

clean:
	rm -f $(OUT)/ctrace $(OUT)/ctrace.bpf.o $(OUT)/ctrace.skel.h

.PHONY: all clean
