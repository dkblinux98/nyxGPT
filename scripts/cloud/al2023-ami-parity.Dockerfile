# AMI-parity image for the containerized cloud artifact-install smoke (#3784).
#
# This image exists to be a *bare Amazon Linux 2023 machine*, not a build
# environment. It adds only what the real AL2023 EC2 AMI already has and the
# stripped container base image does not -- an init system, a login account,
# and the handful of core utilities cloud-init's environment provides. It
# deliberately does NOT install:
#
#   * a modern Python (the AMI's system `python3` is 3.9; nyxGPT requires
#     >=3.11, so selecting an interpreter is the bootstrap's job -- #3782)
#   * node/npm (the web bundle's build needs Node 20; provisioning it is the
#     bootstrap's job -- #3761)
#   * docker (the Cassandra container needs an engine; installing and starting
#     it is the bootstrap's job -- #3760)
#   * git (so a repo checkout is not merely unused at runtime but impossible
#     -- CLAUDE.md's Repo-less Portability requirement)
#
# Every one of those absences is asserted by the smoke's preflight before the
# bootstrap runs: an image that quietly gained any of them would make the
# smoke green by luck, which is the exact failure mode `linux-native-smoke`
# has on this path (D-006/#3753).
#
# Built and driven by `nyxgpt cloud smoke --container`
# (src/nyxgpt/cloud_artifact_smoke.py). Run privileged with a writable cgroup
# mount so systemd (and therefore `systemctl --user`, lingering, and the
# Docker engine the bootstrap installs) behaves as it does on the instance.
ARG BASE_IMAGE=amazonlinux:2023
FROM ${BASE_IMAGE}

# systemd + sudo + the coreutils cloud-init's environment provides. `hostname`
# and `iproute` are not needed by nyxGPT itself but are present on the AMI and
# are what diagnostics reach for when a run fails.
RUN dnf -y install \
        systemd \
        sudo \
        shadow-utils \
        procps-ng \
        iproute \
        hostname \
        tar \
        gzip \
        findutils \
        util-linux \
        which \
        curl-minimal \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# The AMI's default non-root login account. The native install path is
# per-user (systemd --user, never root), so the bootstrap needs a real account
# with a home directory and passwordless sudo, exactly like the AMI's.
#
# `/etc/shadow` is made readable to neutralize a *host* policy, not a target
# one: on an AppArmor host (Ubuntu, and therefore GitHub's runners) the
# `unix-chkpwd` profile attaches to that binary inside the container too and
# denies it `dac_read_search`, so PAM's account stack cannot read root's
# shadow entry and every `sudo -u ec2-user` in the bootstrap fails with
# "Authentication service cannot retrieve authentication info". EC2 has no
# such profile and the real instance is unaffected. Nothing is exposed: every
# account in this throwaway image is password-locked (`!!`), and the container
# is destroyed at the end of the run.
RUN useradd --create-home --shell /bin/bash ec2-user \
    && printf 'ec2-user ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-ec2-user \
    && chmod 0440 /etc/sudoers.d/90-ec2-user \
    && chmod 0644 /etc/shadow

# Where the smoke stages the bootstrap and (optionally) a locally built wheel
# plus the matching nyxgpt-{api,web} service tarballs under artifacts/.
# Deliberately not /tmp: systemd mounts a fresh tmpfs over /tmp as it finishes
# booting, which silently discards anything copied in before that point.
RUN mkdir -p /opt/nyxgpt-smoke/artifacts

# Boot to multi-user like the instance does, and let `docker stop` shut the
# container down the way systemd expects (SIGRTMIN+3 == poweroff).
#
# `systemd-networkd-wait-online` is masked because it cannot succeed here: the
# container's interface is managed by Docker, not networkd, so the unit waits
# out its full timeout and stalls anything ordered after network-online.target
# (the Docker engine the bootstrap starts is). On a real instance networkd
# owns the interface and the unit returns immediately -- this is a
# container-mode substitute for a timing behaviour, not for a tested one.
RUN systemctl set-default multi-user.target \
    && systemctl mask systemd-networkd-wait-online.service
STOPSIGNAL SIGRTMIN+3

CMD ["/usr/lib/systemd/systemd"]
