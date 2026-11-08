// SPDX-License-Identifier: GPL-2.0
/* crosstrace compiled collector: same events as collectors/net_events.bt, emitted as binary records via a ring buffer. */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>
#include "ctrace.h"

char LICENSE[] SEC("license") = "GPL";

const volatile struct ct_cfg cfg = {};

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 16 * 1024 * 1024);
} rb SEC(".maps");

struct owner { __u64 t; __u32 pid, tid; };

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 65536);
	__type(key, __u64);
	__type(value, struct owner);
} own SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, __u32);
	__type(value, __u64);
} dns_t SEC(".maps");

struct gai { __u64 t; char host[48]; };
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, __u32);
	__type(value, struct gai);
} gai_m SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} lost SEC(".maps");

static __always_inline int port_ok(__u16 p)
{
	if (cfg.n_ports == 0)
		return 1;
	for (int i = 0; i < CT_MAX_PORTS; i++) {
		if (i >= cfg.n_ports)
			break;
		if (cfg.ports[i] == p)
			return 1;
	}
	return 0;
}

static __always_inline int dns_ok(__u16 p)
{
	for (int i = 0; i < CT_MAX_PORTS; i++) {
		if (i >= cfg.n_dns)
			break;
		if (cfg.dns_ports[i] == p)
			return 1;
	}
	return 0;
}

static __always_inline struct ct_rec *rec(__u8 type)
{
	struct ct_rec *r = bpf_ringbuf_reserve(&rb, sizeof(*r), 0);
	if (!r) {
		__u32 z = 0;
		__u64 *l = bpf_map_lookup_elem(&lost, &z);
		if (l)
			__sync_fetch_and_add(l, 1);
		return 0;
	}
	__builtin_memset(r, 0, sizeof(*r));
	r->t = bpf_ktime_get_ns();
	r->type = type;
	return r;
}

static __always_inline void who(struct ct_rec *r)
{
	struct bpf_pidns_info ns;
	if (cfg.pidns_ino && !bpf_get_ns_current_pid_tgid(cfg.pidns_dev, cfg.pidns_ino, &ns, sizeof(ns))) {
		r->pid = ns.tgid;       /* same ids the traced program sees (WSL/containers run in a pid namespace) */
		r->tid = ns.pid;
	} else {
		__u64 pt = bpf_get_current_pid_tgid();
		r->pid = pt >> 32;
		r->tid = (__u32)pt;
	}
	bpf_get_current_comm(r->comm, sizeof(r->comm));
}

/* ---- DNS via the stub resolver: UDP sendto() with an address, then the next recvfrom() of that thread ---- */
SEC("tracepoint/syscalls/sys_enter_sendto")
int on_sendto(struct trace_event_raw_sys_enter *ctx)
{
	struct sockaddr_in *sa = (struct sockaddr_in *)ctx->args[4];
	if (!sa)
		return 0;
	struct sockaddr_in a;
	if (bpf_probe_read_user(&a, sizeof(a), sa))
		return 0;
	__u16 port = bpf_ntohs(a.sin_port);
	if (a.sin_family != 2 || !dns_ok(port))
		return 0;
	__u32 tid = (__u32)bpf_get_current_pid_tgid();
	__u64 now = bpf_ktime_get_ns();
	bpf_map_update_elem(&dns_t, &tid, &now, BPF_ANY);
	struct ct_rec *r = rec(CT_DNS_QUERY);
	if (!r)
		return 0;
	who(r);
	r->t = now;
	r->daddr = a.sin_addr.s_addr;
	r->dport = port;
	bpf_ringbuf_submit(r, 0);
	return 0;
}

SEC("tracepoint/syscalls/sys_exit_recvfrom")
int on_recvfrom(struct trace_event_raw_sys_exit *ctx)
{
	__u32 tid = (__u32)bpf_get_current_pid_tgid();
	__u64 *t0 = bpf_map_lookup_elem(&dns_t, &tid);
	if (!t0)
		return 0;
	struct ct_rec *r = rec(CT_DNS_RESPONSE);
	if (!r)
		return 0;
	who(r);
	r->lat_ns = r->t - *t0;
	r->aux = (__u32)ctx->ret;
	bpf_map_delete_elem(&dns_t, &tid);
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* ---- TCP connect start (client side) ---- */
SEC("kprobe/tcp_v4_connect")
int BPF_KPROBE(on_connect, struct sock *sk, struct sockaddr *uaddr)
{
	struct sockaddr_in a;
	if (bpf_probe_read_kernel(&a, sizeof(a), uaddr))
		return 0;
	__u16 port = bpf_ntohs(a.sin_port);
	if (!port_ok(port))
		return 0;
	__u64 k = (__u64)sk;
	struct ct_rec *r = rec(CT_CONNECT_START);
	if (!r)
		return 0;
	who(r);
	struct owner o = { .t = r->t, .pid = r->pid, .tid = r->tid };
	bpf_map_update_elem(&own, &k, &o, BPF_ANY);
	r->daddr = a.sin_addr.s_addr;
	r->dport = port;
	r->sk = k;
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* ---- connection established: fentry on tcp_finish_connect (reliable under load; see decisions) ---- */
SEC("fentry/tcp_finish_connect")
int BPF_PROG(on_connect_end, struct sock *sk, struct sk_buff *skb)
{
	__u16 dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	if (!port_ok(dport))
		return 0;
	__u64 k = (__u64)sk;
	struct ct_rec *r = rec(CT_CONNECT_END);
	if (!r)
		return 0;
	struct owner *o = bpf_map_lookup_elem(&own, &k);
	if (o) {
		r->pid = o->pid;
		r->tid = o->tid;
		r->lat_ns = r->t - o->t;
	}
	r->saddr = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
	r->daddr = BPF_CORE_READ(sk, __sk_common.skc_daddr);
	r->sport = BPF_CORE_READ(sk, __sk_common.skc_num);
	r->dport = dport;
	r->sk = k;
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* socket closed: forget its owner (a reused socket address must not inherit the old pid) */
SEC("fentry/tcp_set_state")
int BPF_PROG(on_state, struct sock *sk, int state)
{
	if (state != 7)
		return 0;
	__u64 k = (__u64)sk;
	bpf_map_delete_elem(&own, &k);
	return 0;
}

/* ---- retransmits (either side) ---- */
SEC("tp_btf/tcp_retransmit_skb")
int BPF_PROG(on_retx, const struct sock *sk, const struct sk_buff *skb)
{
	__u16 sport = BPF_CORE_READ(sk, __sk_common.skc_num);
	__u16 dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	if (!port_ok(sport) && !port_ok(dport))
		return 0;
	struct ct_rec *r = rec(CT_RETRANSMIT);
	if (!r)
		return 0;
	__u64 k = (__u64)sk;
	struct owner *o = bpf_map_lookup_elem(&own, &k);
	if (o) {
		r->pid = o->pid;
		r->tid = o->tid;
	}
	r->saddr = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
	r->daddr = BPF_CORE_READ(sk, __sk_common.skc_daddr);
	r->sport = sport;
	r->dport = dport;
	r->aux = BPF_CORE_READ(sk, __sk_common.skc_state);
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* ---- smoothed RTT whenever a segment is received ---- */
SEC("tp_btf/tcp_probe")
int BPF_PROG(on_probe, const struct sock *sk, const struct sk_buff *skb)
{
	__u16 sport = BPF_CORE_READ(sk, __sk_common.skc_num);
	__u16 dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	if (!port_ok(sport) && !port_ok(dport))
		return 0;
	const struct tcp_sock *tp = (const struct tcp_sock *)sk;
	__u32 srtt = BPF_CORE_READ(tp, srtt_us) >> 3;
	const struct tcphdr *th = (const struct tcphdr *)BPF_CORE_READ(skb, data);
	__u32 len = BPF_CORE_READ(skb, len);
	__u32 doff = 0;
	/* tcp_probe runs with skb->data at the TCP header */
	__u8 b12 = 0;
	bpf_probe_read_kernel(&b12, 1, (const char *)th + 12);
	doff = (b12 >> 4) * 4;
	__u32 data_len = len > doff ? len - doff : 0;
	if (cfg.reduce_rtt && data_len == 0 && srtt <= 20000)
		return 0;
	struct ct_rec *r = rec(CT_RTT);
	if (!r)
		return 0;
	r->saddr = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
	r->daddr = BPF_CORE_READ(sk, __sk_common.skc_daddr);
	r->sport = sport;
	r->dport = dport;
	r->aux = srtt;
	r->aux2 = data_len;
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* ---- optional: every tcp_sendmsg on the watched ports ---- */
SEC("fentry/tcp_sendmsg")
int BPF_PROG(on_send, struct sock *sk, struct msghdr *msg, size_t size)
{
	if (!cfg.tcp_send)
		return 0;
	__u16 sport = BPF_CORE_READ(sk, __sk_common.skc_num);
	__u16 dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	if (!port_ok(sport) && !port_ok(dport))
		return 0;
	struct ct_rec *r = rec(CT_SEND);
	if (!r)
		return 0;
	who(r);
	r->sport = sport;
	r->dport = dport;
	r->aux = (__u32)size;
	bpf_ringbuf_submit(r, 0);
	return 0;
}

/* ---- optional: libc getaddrinfo() as the application sees it (attached by the loader to the libc path) ---- */
SEC("uprobe")
int BPF_KPROBE(on_gai, const char *name)
{
	__u32 tid = (__u32)bpf_get_current_pid_tgid();
	struct gai g = {};
	g.t = bpf_ktime_get_ns();
	bpf_probe_read_user_str(g.host, sizeof(g.host), name);
	bpf_map_update_elem(&gai_m, &tid, &g, BPF_ANY);
	struct ct_rec *r = rec(CT_LIBC_QUERY);
	if (!r)
		return 0;
	who(r);
	r->t = g.t;
	__builtin_memcpy(r->host, g.host, sizeof(r->host));
	bpf_ringbuf_submit(r, 0);
	return 0;
}

SEC("uretprobe")
int BPF_KRETPROBE(on_gai_ret, int ret)
{
	__u32 tid = (__u32)bpf_get_current_pid_tgid();
	struct gai *g = bpf_map_lookup_elem(&gai_m, &tid);
	if (!g)
		return 0;
	struct ct_rec *r = rec(CT_LIBC_RESPONSE);
	if (!r)
		return 0;
	who(r);
	r->lat_ns = r->t - g->t;
	r->aux = ret;
	__builtin_memcpy(r->host, g->host, sizeof(r->host));
	bpf_map_delete_elem(&gai_m, &tid);
	bpf_ringbuf_submit(r, 0);
	return 0;
}
