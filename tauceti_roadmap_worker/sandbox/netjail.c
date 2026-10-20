/* netjail: run a command in a private network namespace whose only way out is the local
 * allow-listing proxy (allowproxy.py) reached over a unix socket.
 *
 *   netjail <command> [args...]
 *
 * Steps: new user+net namespace (root inside, so loopback can be raised) -> loopback up ->
 * a forwarder (nsforward.py) listening on 127.0.0.1:3128 inside the namespace and relaying to
 * $XDG_RUNTIME_DIR/allowproxy.sock on the host -> a nested user namespace that maps back to the
 * real uid (so the command is not root) -> HTTPS_PROXY/HTTP_PROXY/ALL_PROXY set -> exec.
 * Needs an AppArmor profile granting `userns` to this binary on Ubuntu (see netjail.apparmor).
 * The forwarder is $NETJAIL_FORWARDER (the worker points it at its bundled nsforward.py).
 *
 * Build: cc -O2 -Wall -o netjail netjail.c
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static void die(const char *m) { perror(m); exit(111); }

static void writefile(const char *p, const char *s) {
    int fd = open(p, O_WRONLY);
    if (fd < 0) die(p);
    if (write(fd, s, strlen(s)) != (ssize_t)strlen(s)) die(p);
    close(fd);
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: netjail <command> [args...]\n"); return 2; }
    uid_t uid = getuid(); gid_t gid = getgid();
    char buf[64];
    /* 1. user + net namespace, become root inside */
    if (unshare(CLONE_NEWUSER | CLONE_NEWNET) != 0) die("unshare(user+net)");
    writefile("/proc/self/setgroups", "deny");
    snprintf(buf, sizeof buf, "0 %u 1", (unsigned)uid); writefile("/proc/self/uid_map", buf);
    snprintf(buf, sizeof buf, "0 %u 1", (unsigned)gid); writefile("/proc/self/gid_map", buf);
    /* 2. loopback up */
    int s = socket(AF_INET, SOCK_DGRAM, 0); if (s < 0) die("socket");
    struct ifreq ifr; memset(&ifr, 0, sizeof ifr); strcpy(ifr.ifr_name, "lo");
    if (ioctl(s, SIOCGIFFLAGS, &ifr) != 0) die("SIOCGIFFLAGS");
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    if (ioctl(s, SIOCSIFFLAGS, &ifr) != 0) die("SIOCSIFFLAGS");
    close(s);
    /* 3. forwarder inside the namespace (it runs as root of the namespace: tell it the real uid) */
    snprintf(buf, sizeof buf, "%u", (unsigned)uid); setenv("NETJAIL_HOST_UID", buf, 1);
    const char *fwd = getenv("NETJAIL_FORWARDER");
    if (!fwd || !*fwd) { fprintf(stderr, "netjail: set NETJAIL_FORWARDER to the path of nsforward.py\n"); return 2; }
    pid_t f = fork(); if (f < 0) die("fork");
    if (f == 0) { execlp("python3", "python3", "-I", fwd, (char *)NULL); die("exec nsforward"); }
    /* 4. the command, back as the real uid, with proxy variables */
    pid_t w = fork(); if (w < 0) die("fork");
    if (w == 0) {
        if (unshare(CLONE_NEWUSER) != 0) die("unshare(user)");
        writefile("/proc/self/setgroups", "deny");
        snprintf(buf, sizeof buf, "%u 0 1", (unsigned)uid); writefile("/proc/self/uid_map", buf);
        snprintf(buf, sizeof buf, "%u 0 1", (unsigned)gid); writefile("/proc/self/gid_map", buf);
        setenv("HTTPS_PROXY", "http://127.0.0.1:3128", 1); setenv("HTTP_PROXY", "http://127.0.0.1:3128", 1);
        setenv("https_proxy", "http://127.0.0.1:3128", 1); setenv("http_proxy", "http://127.0.0.1:3128", 1);
        setenv("ALL_PROXY", "http://127.0.0.1:3128", 1); setenv("NO_PROXY", "127.0.0.1,localhost", 1);
        setenv("no_proxy", "127.0.0.1,localhost", 1); setenv("NETJAIL", "1", 1);
        /* give the forwarder a moment to bind */
        usleep(300000);
        execvp(argv[1], argv + 1); die("exec command");
    }
    int st = 0; waitpid(w, &st, 0);
    kill(f, SIGTERM); waitpid(f, NULL, 0);
    if (WIFEXITED(st)) return WEXITSTATUS(st);
    if (WIFSIGNALED(st)) return 128 + WTERMSIG(st);
    return 1;
}
