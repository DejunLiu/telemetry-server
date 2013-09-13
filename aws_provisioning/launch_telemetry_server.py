#!/usr/bin/env python
# encoding: utf-8

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from aws_launcher import Launcher
from fabric.api import *
import fabric.network
import sys

class TelemetryServerLauncher(Launcher):
    def nodejs_version(self):
        return "0.10.18"
    def install_nodejs_bin(self):
        node_version = self.nodejs_version()
        run("wget http://nodejs.org/dist/v{0}/node-v{0}-linux-x64.tar.gz".format(node_version))
        run("tar xzvf node-v{0}-linux-x64.tar.gz".format(node_version))
        sudo("mv node-v{0}-linux-x64 /usr/local".format(node_version))

    def install_nodejs_src(self):
        node_version = self.nodejs_version()
        run("wget http://nodejs.org/dist/v{0}/node-v{0}.tar.gz".format(node_version))
        run("tar xzvf node-v{0}.tar.gz".format(node_version))
        with cd("node-v{0}".format(node_version)):
            run("./configure")
            run("make")
            sudo("make install")

    def post_install(self, instance):
        # Install some more:
        self.install_nodejs_src()

        # Create log dir (within base_dir, but symlinked to /var/log):
        base_dir = self.config.get("base_dir", "/mnt/telemetry")
        log_dir = base_dir + "/log"
        run("mkdir {0}".format(log_dir))
        sudo("ln -s {0} /var/log/telemetry".format(log_dir))

        # Create data dir:
        run("mkdir {0}/data".format(base_dir))

        # Install security certificate for running 'process incoming':
        run("mkdir -p ~/.ssh/aws")
        put("~/.ssh/aws/mreid.pem", "~/.ssh/aws/mreid.pem")

        # Increase limits on open files.
        sudo("echo '*                soft    nofile          10000' > /etc/security/limits.conf")
        sudo("echo '*                hard    nofile          30000' >> /etc/security/limits.conf")

        # Each fabric 'run' starts a separate shell, so the limits above should
        # be set correctly. However, we actually need to disconnect from SSH to
        # get a fresh connection first.
        fabric.network.disconnect_all()

        run("echo 'Soft limit:'; ulimit -S -n")
        run("echo 'Hard limit:'; ulimit -H -n")

        # Setup logrotate for the stats log
        lr_file = "/etc/logrotate.d/telemetry"
        sudo("echo '/var/log/telemetry/telemetry-server.log {' > {0}".format(lr_file))
        sudo("echo '    su {1} {1}' >> {0}".format(lr_file, self.ssl_user))
        sudo("echo '    rotate 10' >> {0}".format(lr_file))
        sudo("echo '    daily' >> {0}".format(lr_file))
        sudo("echo '    compress' >> {0}".format(lr_file))
        sudo("echo '    missingok' >> {0}".format(lr_file))
        sudo("echo '    create 640 {1} {1}' >> {0}".format(lr_file, self.ssl_user))
        with settings(warn_only=True):
            # This will warn if there's no file there.
            sudo("logrotate -f /etc/logrotate.d/telemetry")

    def run(self, instance):
        # TODO: daemonize these with an init script or put into screen session
        with cd("telemetry-server/server"):
            run("node ./server.js ./server_config.json")

        # This won't run, since previous will hang.
        print "Telemetry server started"

        # Start up exporter
        with cd("telemetry-server"):
            run("./export.py -d {0}/data -p '^telemetry.log.*[.]finished$' -k '{1}' -s '{2}' -b '{3}' --remove-files --loop".format(base_dir, self.aws_key, self.aws_secret_key, self.config.get("incoming_bucket", "telemetry-incoming")))
        
        # Start up 'process incoming'
        with cd("telemetry-server/aws_provisioning"):
            run("python process_incoming_distributed.py aws_incoming.cc2.8xlarge.json | tee -a process_incoming.log")

def main():
    try:
        launcher = TelemetryServerLauncher()
        launcher.go()
        return 0
    except:
        return 1

if __name__ == "__main__":
    sys.exit(main())
