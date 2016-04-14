# Copyright 2015 Cloudbase Solutions Srl
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.



import collections
import signal
import time

import eventlet

eventlet.monkey_patch()

import oslo_messaging
from oslo_log import log as logging
from oslo_service import loopingcall

from neutron import context
from neutron.agent import rpc as agent_rpc, securitygroups_rpc as sg_rpc
from neutron.common import config as common_config, topics, constants as n_const
from neutron.i18n import _LI, _LW, _LE

from networking_dvs.agent.firewalls import dvs_securitygroup_rpc as dvs_rpc
from networking_dvs.common import constants as dvs_constants, config
from networking_dvs.plugins.ml2.drivers.mech_dvs.agent import vmware_util

LOG = logging.getLogger(__name__)
CONF = config.CONF

class DVSPluginApi(agent_rpc.PluginApi):
    pass


class DvsNeutronAgent(sg_rpc.SecurityGroupAgentRpcCallbackMixin):
    target = oslo_messaging.Target(version='1.4')

    def __init__(self,
                 quitting_rpc_timeout=None,
                 conf=None):

        super(DvsNeutronAgent, self).__init__()

        self.conf = conf or CONF

        self.agent_state = {
            'binary': 'neutron-dvs-agent',
            'host': self.conf.host,
            'topic': n_const.L2_AGENT_TOPIC,
            'configurations': {},
            'agent_type': dvs_constants.AGENT_TYPE_DVS,
            'start_flag': True}

        self.setup_rpc()

        report_interval = 30  # self.conf.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(self._report_state)
            heartbeat.start(interval=report_interval)

        self.polling_interval = 10

        self.enable_security_groups = self.conf.get('SECURITYGROUP', {}).get('enable_security_group', False)
        # Security group agent support
        if self.enable_security_groups:
            self.sg_agent = dvs_rpc.DVSSecurityGroupRpc(self.context,
                                                         self.sg_plugin_rpc,
                                                         local_vlan_map=None,
                                                         integration_bridge=None, # Passed on to FireWall Driver
                                                         defer_refresh_firewall=True)

        self.default_vlan = self.conf.ML2_VMWARE.dv_default_vlan

        self.run_daemon_loop = True
        self.iter_num = 0
        self.fullsync = True

        self.quitting_rpc_timeout = quitting_rpc_timeout


        self.updated_ports = set()
        self.deleted_ports = set()
        self.known_ports = set()
        self.added_ports = set()
        self.booked_ports = set()

        self.network_ports = collections.defaultdict(set)

        self.api = vmware_util.VMWareUtil(self.conf.ML2_VMWARE)
        self.catch_sigterm = False
        self.catch_sighup = False
        self.connection.consume_in_threads()

    def port_update(self, context, **kwargs):
        port = kwargs.get('port')
        port_id = port['id']
        if port_id in self.known_ports: # Avoid updating a port, which has not been created yet
            self.updated_ports.add(port_id)
        LOG.info("port_update message processed for port".format(port_id))

    def port_delete(self, context, **kwargs):
        port_id = kwargs.get('port_id')
        self.updated_ports.discard(port_id)
        # self.added_ports.discard(port_id)
        self.known_ports.discard(port_id)
        self.deleted_ports.add(port_id)
        LOG.debug("port_delete message processed for port %s", port_id)


    def network_create(self, context, **kwargs):
        LOG.info(_LI("Agent network_create"))

    def network_update(self, context, **kwargs):
        network_id = kwargs['network']['id']
        for port_id in self.network_ports[network_id]:
            # notifications could arrive out of order, if the port is deleted
            # we don't want to update it anymore
            if port_id not in self.deleted_ports:
                self.updated_ports.add(port_id)
        LOG.debug("Agent network_update for network "
                  "%(network_id)s, with ports: %(ports)s",
                  {'network_id': network_id,
                   'ports': self.network_ports[network_id]})

    def network_delete(self, context, **kwargs):
        LOG.info(_LI("Agent network_delete"))

    def _clean_network_ports(self, port_id):
        for port_set in self.network_ports.values():
            if port_id in port_set:
                port_set.remove(port_id)
                break

    def setup_rpc(self):
        self.agent_id = 'dvs-agent-%s' % self.conf.host
        self.topic = topics.AGENT
        self.plugin_rpc = DVSPluginApi(topics.PLUGIN)
        self.sg_plugin_rpc = sg_rpc.SecurityGroupServerRpcApi(topics.PLUGIN)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)

        # RPC network init
        self.context = context.get_admin_context_without_session()

        # Handle updates from service
        self.endpoints = [self]

        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.CREATE],
                     [topics.PORT, topics.UPDATE],
                     [topics.PORT, topics.DELETE],
                     [topics.NETWORK, topics.CREATE],
                     [topics.NETWORK, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE],
                     [topics.SECURITY_GROUP, topics.UPDATE]]

        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers,
                                                     start_listening=False)

    def _report_state(self):
        # LOG.info(_LI("******** Reporting state via rpc"))

        try:
            self.state_rpc.report_state(self.context,
                                        self.agent_state)

            self.agent_state.pop('start_flag', None)
            # LOG.info(_LI("******** Reporting state completed"))
        except Exception:
            # LOG.info(_LI("******** Reporting state via rpc failed "))
            LOG.exception(_LE("Failed reporting state!"))

    def _check_and_handle_signal(self):
        if self.catch_sigterm:
            LOG.info(_LI("Agent caught SIGTERM, quitting daemon loop."))
            self.run_daemon_loop = False
            self.catch_sigterm = False

        if self.catch_sighup:
            LOG.info(_LI("Agent caught SIGHUP, resetting."))
            self.conf.reload_config_files()
            common_config.setup_logging()
            LOG.debug('Full set of CONF:')
            self.conf.log_opt_values(LOG, logging.DEBUG)
            self.catch_sighup = False

        return self.run_daemon_loop

    def _handle_sigterm(self, signum, frame):
        self.catch_sigterm = True
        if self.quitting_rpc_timeout:
            self.set_rpc_timeout(self.quitting_rpc_timeout)

    def _handle_sighup(self, signum, frame):
        self.catch_sighup = True

    @staticmethod
    def _to_list_of_neutron_ports(ports):
        neutron_ports = set()

        for mac, port in ports.iteritems():
            neutron_ports.add(port['neutron_info']['port_id'])

        return neutron_ports

    @staticmethod
    def _to_list_of_macs(ports):

        return ports.keys()

    @staticmethod
    def _unbound_ports(ports):
        unbound_ports = {}

        for mac, port in ports.iteritems():
            if "neutron_info" in port and port["connected_vlan"] != port['neutron_info']['segmentation_id']:
                unbound_ports[mac] = port

        return unbound_ports

    def _scan_ports(self, full_scan=False):
        start = time.clock()
        ports = self.api.get_connected_ports_on_dvpg(True)

        LOG.info("dvpg ports {}".format(ports))

        macs = []
        for mac, port_info in ports.iteritems():
            if port_info["connected_vlan"] == self.default_vlan:
                macs.append(mac)

        LOG.info("Macs {}".format(macs))

        neutron_ports = self.plugin_rpc.get_devices_details_list(self.context, devices=macs, agent_id=self.agent_id,
                                                                 host=self.conf.host)

        LOG.info("Neutron ports {}".format(neutron_ports))

        for neutron_info in neutron_ports:
            if neutron_info and "mac_address" in neutron_info and ports[neutron_info["mac_address"]] is not None:
                ports[neutron_info["mac_address"]]["neutron_info"] = neutron_info

        LOG.info(_LI("Scan ports completed in {} seconds".format(time.clock() - start)))

        return ports

    def _bind_ports(self, added_ports):

        devices_up = []
        devices_down = []

        for mac, port in added_ports.iteritems():
            if port["connected_vlan"] != port['neutron_info']['segmentation_id']:

                LOG.info("Preparing to bind port {} to VLAN {}".format(port['neutron_info']['port_id'],
                                                                       port['neutron_info']['segmentation_id']))

                try:
                    self.api.bind_port(port)
                    devices_up.append(port['neutron_info']['port_id'])
                except Exception:
                    devices_down.append(port['neutron_info']['port_id'])

        LOG.info("Updating ports up {} down {} agent {} host {}".format(devices_up, devices_down, self.agent_id,
                                                                        self.conf.host))

        result = self.plugin_rpc.update_device_list(self.context, devices_up, devices_down, self.agent_id,
                                                    self.conf.host)

        LOG.info("Updated ports result {}".format(result))


    def loop_count_and_wait(self, start_time, port_stats):
        # sleep till end of polling interval
        elapsed = time.clock() - start_time
        LOG.debug("Agent rpc_loop - iteration:%(iter_num)d "
                  "completed. Processed ports statistics: "
                  "%(port_stats)s. Elapsed:%(elapsed).3f",
                  {'iter_num': self.iter_num,
                   'port_stats': port_stats,
                   'elapsed': elapsed})

        if elapsed < self.polling_interval:
            time.sleep(self.polling_interval - elapsed)
        else:
            LOG.debug("Loop iteration exceeded interval "
                      "(%(polling_interval)s vs. %(elapsed)s)!",
                      {'polling_interval': self.polling_interval,
                       'elapsed': elapsed})
        self.iter_num = self.iter_num + 1

    def process_ports(self):
        LOG.info("******* Processing Ports *******")
        deleted_ports = self.deleted_ports.copy()
        if deleted_ports:
            # Nothing really to do on the VCenter - we let the vcenter unplug - so all we need to do is
            # trigger the firewall update and clear the deleted ports list
            self.sg_agent.remove_devices_filter(deleted_ports)
            self.deleted_ports = self.deleted_ports - deleted_ports # This way we miss fewer concurrent update

        # Get current ports known on the VMWare integration bridge
        ports = self._scan_ports(self.fullsync)
        unbound_ports = DvsNeutronAgent._unbound_ports(ports)
        if unbound_ports:
            self._bind_ports(unbound_ports)
        added_ports = DvsNeutronAgent._to_list_of_neutron_ports(unbound_ports)

        updated_ports = self.updated_ports.copy()
        self.updated_ports = self.updated_ports - updated_ports

        # update firewall agent if we have added or updated ports
        if self.updated_ports or added_ports:
            self.sg_agent.setup_port_filters(added_ports, updated_ports)

        self.known_ports |= added_ports

        return {
            'added': len(added_ports),
            'updated': len(updated_ports),
            'removed': len(deleted_ports)
        }


    def rpc_loop(self):
        while self._check_and_handle_signal():
            start = time.clock()
            port_stats = {'regular': {'added': 0,
                                      'updated': 0,
                                      'removed': 0}}


            port_stats['regular'] = self.process_ports()

            if self.fullsync:
                self.fullsync = False

            self.loop_count_and_wait(start, port_stats)

    def daemon_loop(self):
        # Start everything.
        LOG.info(_LI("Agent initialized successfully, now running... "))
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, self._handle_sighup)
            self.rpc_loop()


def main():
    import sys
    common_config.init(sys.argv[1:])
    common_config.setup_logging()

    agent = DvsNeutronAgent()

    # Start everything.
    LOG.info(_LI("Agent initialized successfully, now running... "))
    agent.daemon_loop()


if __name__ == "__main__":
    main()
