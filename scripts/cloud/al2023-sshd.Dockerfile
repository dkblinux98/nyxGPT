# A bare Amazon Linux 2023 box that answers SSH -- the target half of
# `scripts/cloud-dev-deploy-smoke.sh` (#3950).
#
# `nyxgpt cloud deploy --dev` copies the operator's working tree to the
# instance over its own SSH connection and installs it there. Everything about
# that claim is a property of two machines and the link between them -- the
# archive's shape, the remote command's quoting, where the tree lands, and
# what the instance's venv then imports -- so a unit test structurally cannot
# see it. This image is the far end.
#
# Deliberately close to `al2023-ami-parity.Dockerfile` (the artifact smoke's
# image) rather than a convenient Ubuntu: the AMI the compute module resolves
# is Amazon Linux 2023, and the five serial rc9 cloud defects were all "what
# happens to exist on the target machine" problems. What is added here is only
# what makes the box reachable and able to build a venv -- sshd, a login user,
# and a Python that satisfies nyxGPT's >=3.11 floor. Nothing else: no git (the
# instance must never need one), no node, no docker.
FROM amazonlinux:2023

RUN dnf install -y --setopt=install_weak_deps=False \
        openssh-server shadow-utils sudo tar gzip findutils \
        python3.11 python3.11-pip \
    && dnf clean all \
    && ssh-keygen -A

# The AMI's default login account, and the user `--ssh-user` defaults to.
RUN useradd -m -s /bin/bash ec2-user \
    && mkdir -p /home/ec2-user/.ssh \
    && chmod 700 /home/ec2-user/.ssh \
    && chown -R ec2-user:ec2-user /home/ec2-user/.ssh

# Key-only, exactly as `SSH_COMMON_OPTIONS`' `BatchMode=yes` requires: a
# password prompt here would hang the smoke instead of failing it.
RUN sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
