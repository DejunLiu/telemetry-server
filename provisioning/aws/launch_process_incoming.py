#!/usr/bin/env python
# encoding: utf-8

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import time
from fabric.api import *
import sys
import aws_util
import traceback
from aws_launcher import Launcher


class ProcessIncomingLauncher(Launcher):
    def post_install(self, instance):
        self.install_histogram_tools(instance)
        with cd(self.config.get("base_dir", "/mnt/telemetry")):
            run("mkdir work processed")

        self.create_log_dir()
        # create startup scripts:
        c_file = "/etc/init/telemetry-incoming.conf"
        self.start_suid_script(c_file, self.ssl_user)
        sudo("echo '    cd {1}' >> {0}".format(c_file, code_base))
        # FIXME:
        #  - run process_incoming_mp instead of process_incoming_queue
        #  - add "--loop" feature
        #  - exit cleanly on SIGINT
        #  - write to log in discrete pieces so we can more easily logrotate

        # Use unbuffered output (-u) so we can see things in the log
        # immediately.
        sudo("echo \"    /usr/bin/python -u -m provisioning.aws.process_incoming_queue -k '{1}' -s '{2}' provisioning/aws/aws_incoming.json >> /var/log/telemetry/telemetry-incoming.out\" >> {0}".format(c_file, self.aws_key, self.aws_secret_key))
        self.end_suid_script(c_file)
        sudo("echo 'kill signal INT' >> {0}".format(c_file))
        sudo("echo 'start on runlevel [2345]' >> {0}".format(c_file))
        sudo("echo 'stop on runlevel [016]' >> {0}".format(c_file))
        # Wait up to 5 minutes for the current batch to finish.
        sudo("echo 'kill timeout 300' >> {0}".format(c_file))

    def run(self, instance):
        # Startup 'process_incoming' service:
        sudo("start telemetry-incoming")
        print "Telemetry 'process incoming' service started."

def main():
    try:
        launcher = ProcessIncomingLauncher()
        launcher.go()
        return 0
    except Exception, e:
        print "Error:", e
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
