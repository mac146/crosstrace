// SPDX-License-Identifier: GPL-2.0
/* Loader for the compiled collector. REQUIRES ROOT. Streams binary records (see ctrace.h) to stdout.
 *   ctrace [--port N]... [--dns-port N]... [--reduce-rtt] [--tcp-send] [--libc-dns PATH]
 * Stream: 16-byte header {"CTRC", u32 record_size, u64 monotonic_ns at start}, then fixed-size records.
 * On SIGINT/SIGTERM it drains the ring buffer, writes "lost=N" to stderr and exits. */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "ctrace.h"
#include "ctrace.skel.h"

static volatile sig_atomic_t stop;
static void on_sig(int s) { (void)s; stop = 1; }

static int on_rec(void *ctx, void *data, size_t len)
{
	(void)ctx;
	if (len != sizeof(struct ct_rec))
		return 0;
	fwrite(data, 1, len, stdout);
	return 0;
}

static int quiet_log(enum libbpf_print_level l, const char *f, va_list a)
{
	if (l == LIBBPF_WARN)
		return vfprintf(stderr, f, a);
	return 0;
}

static unsigned long long mono(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (unsigned long long)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

int main(int argc, char **argv)
{
	struct ct_cfg cfg = {};
	const char *libc = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--port") && i + 1 < argc && cfg.n_ports < CT_MAX_PORTS)
			cfg.ports[cfg.n_ports++] = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--dns-port") && i + 1 < argc && cfg.n_dns < CT_MAX_PORTS)
			cfg.dns_ports[cfg.n_dns++] = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--reduce-rtt"))
			cfg.reduce_rtt = 1;
		else if (!strcmp(argv[i], "--tcp-send"))
			cfg.tcp_send = 1;
		else if (!strcmp(argv[i], "--libc-dns") && i + 1 < argc)
			libc = argv[++i];
		else {
			fprintf(stderr, "usage: ctrace [--port N]... [--dns-port N]... [--reduce-rtt] [--tcp-send] [--libc-dns PATH]\n");
			return 2;
		}
	}
	struct stat st;
	if (!stat("/proc/self/ns/pid", &st)) {
		cfg.pidns_dev = st.st_dev;
		cfg.pidns_ino = st.st_ino;
	}
	libbpf_set_print(quiet_log);
	struct ctrace_bpf *sk = ctrace_bpf__open();
	if (!sk) {
		fprintf(stderr, "open failed\n");
		return 1;
	}
	memcpy((void *)&sk->rodata->cfg, &cfg, sizeof(cfg));
	if (!cfg.tcp_send)
		bpf_program__set_autoload(sk->progs.on_send, false);
	if (!libc) {
		bpf_program__set_autoload(sk->progs.on_gai, false);
		bpf_program__set_autoload(sk->progs.on_gai_ret, false);
	}
	if (cfg.n_dns == 0) {
		bpf_program__set_autoload(sk->progs.on_sendto, false);
		bpf_program__set_autoload(sk->progs.on_recvfrom, false);
	}
	if (ctrace_bpf__load(sk)) {
		fprintf(stderr, "load failed (verifier or kernel support)\n");
		return 1;
	}
	/* attach each program (the skeleton's attach-all cannot do uprobes without a target) */
	struct bpf_program *p;
	bpf_object__for_each_program(p, sk->obj) {
		if (!bpf_program__autoload(p))
			continue;
		const char *sec = bpf_program__section_name(p);
		if (!strcmp(sec, "uprobe") || !strcmp(sec, "uretprobe")) {
			LIBBPF_OPTS(bpf_uprobe_opts, o, .func_name = "getaddrinfo",
				    .retprobe = !strcmp(sec, "uretprobe"));
			struct bpf_link *l = bpf_program__attach_uprobe_opts(p, -1, libc, 0, &o);
			if (!l) {
				fprintf(stderr, "attach %s failed\n", bpf_program__name(p));
				return 1;
			}
		} else if (!bpf_program__attach(p)) {
			fprintf(stderr, "attach %s failed\n", bpf_program__name(p));
			return 1;
		}
	}
	struct ring_buffer *rb = ring_buffer__new(bpf_map__fd(sk->maps.rb), on_rec, NULL, NULL);
	if (!rb) {
		fprintf(stderr, "ring buffer failed\n");
		return 1;
	}
	signal(SIGINT, on_sig);
	signal(SIGTERM, on_sig);
	static char buf[1 << 16];
	setvbuf(stdout, buf, _IOFBF, sizeof(buf));
	struct { char magic[4]; unsigned int size; unsigned long long t; } hdr = {{'C', 'T', 'R', 'C'}, sizeof(struct ct_rec), mono()};
	fwrite(&hdr, 1, sizeof(hdr), stdout);
	fflush(stdout);
	while (!stop) {
		int n = ring_buffer__poll(rb, 50);
		if (n < 0 && n != -EINTR)
			break;
		fflush(stdout);
	}
	ring_buffer__consume(rb);
	fflush(stdout);
	unsigned int z = 0;
	unsigned long long lost = 0;
	bpf_map_lookup_elem(bpf_map__fd(sk->maps.lost), &z, &lost);
	fprintf(stderr, "lost=%llu\n", lost);
	return 0;
}
