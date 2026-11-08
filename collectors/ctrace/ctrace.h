/* Shared between the BPF program and the loader: one fixed-size record per event (little endian, 120 bytes).
 * Python decoder format: "<QQQ II II II HH B3x 16s 48s"  (collectors/cebpf.py must match). */
#ifndef CTRACE_H
#define CTRACE_H

enum ct_type {
	CT_DNS_QUERY = 1,
	CT_DNS_RESPONSE = 2,
	CT_CONNECT_START = 3,
	CT_CONNECT_END = 4,
	CT_RETRANSMIT = 5,
	CT_RTT = 6,
	CT_SEND = 7,
	CT_LIBC_QUERY = 8,
	CT_LIBC_RESPONSE = 9,
};

struct ct_rec {
	unsigned long long t;       /* CLOCK_MONOTONIC ns */
	unsigned long long sk;      /* socket pointer (connect events) */
	unsigned long long lat_ns;
	unsigned int pid, tid;
	unsigned int saddr, daddr;  /* as in the packet (network order bytes in a little-endian u32) */
	unsigned int aux, aux2;     /* retransmit: state; rtt: srtt_us,bytes; send: bytes; dns_response: bytes; libc_response: ret */
	unsigned short sport, dport; /* host order */
	unsigned char type;
	unsigned char pad[3];
	char comm[16];              /* task name of the caller (events with a pid) */
	char host[48];              /* libc getaddrinfo name */
};

#define CT_MAX_PORTS 16

struct ct_cfg {
	unsigned int n_ports;
	unsigned int n_dns;
	unsigned int reduce_rtt;
	unsigned int tcp_send;
	unsigned long long pidns_dev, pidns_ino;   /* the loader's pid namespace: ids are reported as the loader sees them */
	unsigned short ports[CT_MAX_PORTS];
	unsigned short dns_ports[CT_MAX_PORTS];
};

#endif
