# Copyright 2015 Mirantis, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import atexit
import attr
import hashlib
import itertools
import time
import uuid

import six
from eventlet import sleep
from oslo_log import log
from oslo_utils import timeutils
from pyVim.connect import SmartConnect, SmartConnectNoSSL, Disconnect
from pyVim.task import WaitForTask as wait_for_task
from pyVmomi import vim, vmodl
from collections import Counter
from requests.exceptions import ConnectionError

from networking_dvs.common import config, util
from networking_dvs.common import constants as dvs_const
from networking_dvs.common import exceptions
from networking_dvs.common.util import stats, optional_attr
from networking_dvs.utils import spec_builder as builder
from neutron.common import utils as neutron_utils
from neutron.i18n import _LI, _LW, _LE
from osprofiler.profiler import trace_cls

LOG = log.getLogger(__name__)

INIT_PG_PORTS_COUNT = 4

CONF = config.CONF


def wrap_retry(func):
    """
    Retry operation on dvs when concurrent modification by another operation
    was discovered
    """

    @six.wraps(func)
    def wrapper(*args, **kwargs):
        login_failures = 0
        while True:
            try:
                return func(*args, **kwargs)
            except vim.fault.InvalidLogin as e:
                if (vim.fault.InvalidLogin and
                        login_failures < dvs_const.LOGIN_RETRIES - 1):
                    login_failures += 1
                    continue
                else:
                    raise e
            except (vim.fault.VimFault,
                    exceptions.VMWareDVSException) as e:
                if dvs_const.CONCURRENT_MODIFICATION_TEXT in str(e):
                    continue
                raise e

    return wrapper


def dvportgroup_name(uuid, sg_set):
    """
    Returns a dvportgroup name for the particular security group set
    in the context of the switch of the given uuid
    """
    # There is an upper limit on managed object names in vCenter
    dvs_id = ''.join(uuid.split(' '))[:8]
    name = sg_set + "-" + dvs_id
    if len(name) > 80:
        # so we use a hash of the security group set
        hex = hashlib.sha224()
        hex.update(sg_set)
        name = hex.hexdigest() + "-" + dvs_id

    return name


def _config_differs(current, update):
    if current.__class__ != update.__class__:
        return True

    # Technically, that is the case, but we have practically only
    # missing entries, because we do not get the default values from the api,
    # but the constructor sets them
    # missing = set(update.__keylist__) - set(current.__keylist__)
    # if missing:
    #     return True

    for name, value in six.iteritems(current.__dict__):
        try:
            new_value = getattr(update, name)

            if not new_value or hasattr(new_value, 'inherited') and new_value.inherited is None:
                continue

            if isinstance(value, list):
                if len(value) != len(new_value):
                    return True

                for a, b in itertools.izip(value, new_value):
                    if _config_differs(a, b):
                        return True
                continue

            if isinstance(value, vmodl.DynamicData):
                if _config_differs(value, new_value):
                    return True
                else:
                    continue

            if value != new_value:
                return True
        except KeyError:
            pass

    return False

@attr.s(**dvs_const.ATTR_ARGS)
class PortGroup(object):
    ref = attr.ib()
    key = attr.ib(convert=str)
    name = attr.ib(convert=str)
    description = attr.ib(convert=str)
    config_version = attr.ib(default=None, convert=optional_attr(str), hash=False, cmp=False) # Actually an int, but represented as a string
    vlans = attr.ib(default=attr.Factory(Counter), hash=False, cmp=False)
    default_port_config = attr.ib(default=None, hash=False, repr=False, cmp=False)
    async_fetch = attr.ib(default=None, hash=False, repr=False, cmp=False)


@trace_cls("vmwareapi", hide_args=True)
class DVSController(object):
    """Controls one DVS."""

    def __init__(self, dvs_name, connection=None, pool=None, rectify_wait=120):
        self.connection = connection
        self.dvs_name = dvs_name
        self.max_mtu = None
        self.pool = pool
        self._update_spec_queue = []
        self.ports_by_key = {}
        self._blocked_ports = set()
        self.hosts_to_rectify = {}
        self.rectify_wait = rectify_wait

        try:
            self._dvs, self._datacenter = self._get_dvs(dvs_name, connection)
            # (SlOPS) To do release blocked port after use
            self._blocked_ports = set()
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

        dvs_config = self._dvs.config
        self._uuid = dvs_config.uuid
        self._max_mtu = dvs_config.maxMtu
        self._config_version = dvs_config.configVersion

        self._port_groups_by_name = {}
        # self._get_portgroups(refresh=True)

    @property
    def uuid(self):
        return self._uuid

    @property
    def mtu(self):
        return self._max_mtu

    def update_mtu(self, max_mtu):
        if max_mtu == self._max_mtu:
            return
        try:
            pg_config_info = self._build_dvswitch_update_spec()
            pg_config_info.maxMtu = max_mtu
            pg_config_info.configVersion = self._config_version

            pg_update_task = self._dvs.ReconfigureDvs_Task(spec=pg_config_info)

            wait_for_task(pg_update_task, si=self.connection)
            self.max_mtu = max_mtu
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def create_network(self, network, segment):
        name = self._get_net_name(self.dvs_name, network)
        blocked = not network['admin_state_up']

        try:
            pg_spec = self._build_pg_create_spec(
                name,
                segment['segmentation_id'],
                blocked)
            pg_create_task = self._dvs.CreateDVPortgroup_Task(spec=pg_spec)

            result = wait_for_task(pg_create_task, si=self.connection)
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)
        else:
            pg = result.result
            self._port_groups_by_name[name] = pg
            LOG.info(_LI('Network %(name)s created \n%(pg_ref)s'),
                     {'name': name, 'pg_ref': pg})
            return pg

    def update_network(self, network, original=None):
        original_name = self._get_net_name(self.dvs_name, original) if original else None
        current_name = self._get_net_name(self.dvs_name, network)
        blocked = not network['admin_state_up']
        try:
            pg_ref = self._get_pg_by_name(original_name or current_name)
            pg_config_info = self._get_config_by_ref(pg_ref)

            if (pg_config_info.defaultPortConfig.blocked.value != blocked or
                    (original_name and original_name != current_name)):
                # we upgrade only defaultPortConfig, because it is inherited
                # by all ports in PortGroup, unless they are explicitly
                # overwritten on specific port.
                pg_spec = self._build_pg_update_spec(
                    pg_config_info.configVersion,
                    blocked=blocked)
                pg_spec.name = current_name
                pg_update_task = pg_ref.ReconfigureDVPortgroup_Task(spec=pg_spec)

                wait_for_task(pg_update_task, si=self.connection)
                LOG.info(_LI('Network %(name)s updated'),
                         {'name': current_name})
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def delete_network(self, network):
        name = self._get_net_name(self.dvs_name, network)
        try:
            pg_ref = self._get_pg_by_name(name)
        except exceptions.PortGroupNotFound:
            LOG.debug('Network %s is not present in vcenter. '
                      'Nothing to delete.' % name)
            return
        self._delete_port_group(pg_ref, name)

    def delete_networks_without_active_ports(self, pg_keys_with_active_ports):
        for pg_ref in self._get_all_port_groups():
            if pg_ref.value not in pg_keys_with_active_ports:
                # check name
                try:
                    name = util.get_object_property(self.connection, pg_ref, 'name')
                    name_tokens = name.split(self.dvs_name)
                    if (len(name_tokens) == 2 and not name_tokens[0] and
                            self._valid_uuid(name_tokens[1])):
                        self._delete_port_group(pg_ref, name)
                except vim.fault.VimFault as e:
                    if dvs_const.DELETED_TEXT in e.message:
                        pass

    @stats.timed()
    def _delete_port_group(self, pg_ref, name, ignore_in_use=False):
        while True:
            try:
                pg_delete_task = pg_ref.Destroy_Task()
                wait_for_task(pg_delete_task, si=self.connection)
                LOG.info(_LI('Network %(name)s deleted.') % {'name': name})
                self._port_groups_by_name.pop(name, None)
                return True
            except vim.fault.ResourceInUse as e:
                if ignore_in_use:
                    LOG.info(_LW("Could not delete port-group %(name)s. Reason: %(message)s")
                             % {'name': name, 'message': e.message})
                    return False
                else:
                    raise exceptions.wrap_wmvare_vim_exception(e)
            except vim.fault.VimFault as e:
                if dvs_const.DELETED_TEXT in e.message:
                    return True
                else:
                    raise exceptions.wrap_wmvare_vim_exception(e)
        return False

    def submit_update_ports(self, update_specs):
        return self._dvs.ReconfigureDVPort_Task(port=update_specs)

    def update_ports(self, update_specs):
        if not update_specs:
            return
        LOG.debug("Update Ports: {}".format(sorted([spec.name for spec in update_specs])))
        update_task = self.submit_update_ports(update_specs)
        try:
            return wait_for_task(update_task, si=self.connection)  # -> May raise DvsOperationBulkFault, when host is down
        except vim.fault.NotFound:
            return

    def queue_update_specs(self, update_specs, callback=None):
        self._update_spec_queue.append((update_specs, [callback]))
        stats.gauge('networking_dvs.update_spec_queue_length', len(self._update_spec_queue))

    def filter_update_specs(self, filter_func):
        self._update_spec_queue = [
            (filter(filter_func, update_specs), callbacks)
            for update_specs, callbacks in self._update_spec_queue]

    @staticmethod
    def _chunked_update_specs(specs, limit=500):
        specs = list(specs)
        countdown = limit
        first = 0
        for i, spec in enumerate(specs):
            try:
                for filter_config in spec.setting.filterPolicy.filterConfig:
                    countdown -= len(filter_config.parameters)
                    try:
                        countdown -= len(filter_config.trafficRuleset)
                    except AttributeError:
                        pass
            except AttributeError:
                pass
            if countdown <= 0:
                last = i + 1
                yield (specs[first:last])
                countdown = limit
                first = last
        yield (specs[first:])

    def apply_queued_update_specs(self):
        callbacks, update_specs_by_key = self._get_queued_update_changes()

        if not update_specs_by_key:
            return

        results = []
        for result in self.pool.starmap(self._apply_queued_update_specs, [(update_spec, callbacks) for update_spec in
                                                                          self._chunked_update_specs(six.itervalues(
                                                                              update_specs_by_key))]):
            if result:
                results.extend(result)

        return results

    def _apply_queued_update_specs(self, update_specs, callbacks, retries=5):
        if not update_specs:
            return

        failed_keys = []
        for i in range(retries):
            try:
                value = self.update_ports(update_specs)

                for spec in update_specs:
                    port = self.ports_by_key[spec.key]
                    port_desc = port.get('port_desc', None)
                    if port_desc and port_desc.config_version:
                        port_desc.config_version = str(int(port_desc.config_version) + 1)

                if callbacks:
                    succeeded_keys = [str(spec.key) for spec in update_specs]
                for callback in callbacks:
                    if callable(callback):
                        callback(self, succeeded_keys, failed_keys)

                return value
            except (vim.fault.DvsOperationBulkFault, vim.fault.NoHost) as e:
                # We log it as error, but do not fail, so that the agent doesn't have to restart
                LOG.error("Failed to apply changes due to: %s", e.msg)
            except vim.fault.VimFault as e:
                if dvs_const.CONCURRENT_MODIFICATION_TEXT in e.msg:
                    for port_info in self.get_port_info_by_portkey([spec.key for spec in update_specs]):
                        port_key = str(port_info.key)
                        port = self.ports_by_key[port_key]
                        port_desc = port['port_desc']
                        update_spec_index = None
                        update_spec = None

                        for index, item in enumerate(update_specs):
                            if item.key == port_key:
                                update_spec = item
                                update_spec_index = index
                                break

                        connection_cookie = getattr(port_info, "connectionCookie", None)

                        if connection_cookie:
                            connection_cookie = str(connection_cookie)

                        if connection_cookie != port_desc.connection_cookie:
                            LOG.error("Cookie mismatch {} {} {} <> {}".format(port_desc.mac_address, port_desc.port_key,
                                                                              port_desc.connection_cookie,
                                                                              connection_cookie))
                            if update_spec_index:
                                failed_keys.append(port_key)
                                del update_specs[update_spec_index]
                        else:
                            config_version = str(port_info.config.configVersion)
                            port_desc.config_version = config_version
                            if update_spec:
                                LOG.debug("Config version {} {} from {} ({}) to {}".format(port_desc.mac_address,
                                                                                           port_desc.port_key,
                                                                                           port_desc.config_version,
                                                                                           update_spec.configVersion,
                                                                                           config_version))

                                update_spec.configVersion = config_version
                    continue

                raise exceptions.wrap_wmvare_vim_exception(e)

    def _get_queued_update_changes(self):
        callbacks = []
        # First merge the changes for the same port(key)
        # Later changes overwrite earlier ones, non-inherited values take precedence
        # This cannot be called out-of-order
        update_specs_by_key = {}
        update_spec_queue = self._update_spec_queue
        self._update_spec_queue = []
        stats.gauge('networking_dvs.update_spec_queue', len(self._update_spec_queue))

        for _update_specs, _callbacks in update_spec_queue:
            if _callbacks:
                callbacks.extend(_callbacks)

            for spec in _update_specs:
                existing_spec = update_specs_by_key.get(spec.key, None)
                if not existing_spec:
                    update_specs_by_key[spec.key] = spec
                else:
                    for attr in ['configVersion', 'description', 'name']:
                        value = getattr(spec, attr, None)
                        if not value is None and value != getattr(existing_spec, attr, None):
                            setattr(existing_spec, attr, value)
                    for attr in ['blocked', 'filterPolicy', 'vlan']:
                        value = getattr(spec.setting, attr)
                        if not value.inherited is None:
                            setattr(existing_spec.setting, attr, getattr(spec.setting, attr))
        return callbacks, update_specs_by_key

    def get_port_group_for_security_group_set(self, security_group_set, max_objects=100):
        portgroups = self._get_portgroups(max_objects)

        for dvpg in six.itervalues(portgroups):
            if dvpg.description == security_group_set:
                return dvpg

    def _get_portgroups(self, max_objects=100, refresh=False):
        if self._port_groups_by_name and not refresh:
            return self._port_groups_by_name

        """Get all portgroups on the switch"""
        si = self.connection
        property_collector = si.content.propertyCollector

        traversal_spec = util.build_traversal_spec(
            "dvs_to_dvpg",
            vim.DistributedVirtualSwitch,
            "portgroup",
            False,
            [])
        object_spec = util.build_object_spec(
            self._dvs,
            [traversal_spec])
        property_spec = util.build_property_spec(
            vim.DistributedVirtualPortgroup,
            ["key", "name", "config.description"])

        property_filter_spec = util.build_property_filter_spec(
            [property_spec],
            [object_spec])
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        options.maxObjects = max_objects

        pc_result = property_collector.RetrievePropertiesEx(specSet=[property_filter_spec], options=options)

        def _fetch(pg):
            sleep(0) # Yield to another task
            result = util.get_object_properties_dict(self.connection, pg.ref,
                                                ["config.configVersion", "config.defaultPortConfig"])
            pg.config_version = result.pop("config.configVersion", None)
            pg.default_port_config = result.pop("config.defaultPortConfig", None)
            pg.async_fetch = None

        with util.WithRetrieval(vim, pc_result) as pc_objects:
            for objContent in pc_objects:
                props = {prop.name: prop.val for prop in objContent.propSet}
                pg = PortGroup(ref=objContent.obj,
                               key=props["key"],
                               name=props["name"],
                               description=props.pop("config.description", ""),
                               config_version=props.pop("config.configVersion", None),
                               default_port_config=props.pop("config.defaultPortConfig", None),
                               )

                if not pg.config_version or not pg.default_port_config:
                    pg.async_fetch = self.pool.spawn(_fetch, pg)
                self._port_groups_by_name[pg.name] = pg

        return self._port_groups_by_name

    @stats.timed()
    def create_dvportgroup(self, sg_set, port_config, update=True):
        """
        Creates an automatically-named dvportgroup on the dvswitch
        with the specified sg rules and marks it as such through the description

        Returns a dictionary with "key" and "ref" keys.

        Note, that while a portgroup's key and managed object id have
        the same string format and appear identical under normal use
        it is possible to have them diverge by the use of the backup
        and restore feature of the dvs for example.
        As such, one should not rely on any equivalence between them.
        """
        # There is a create_network method a few lines above
        # which seems to be part of a non-used call path
        # starting from the dvs_agent_rpc_api. TODO - remove it

        dvpg_name = dvportgroup_name(self.uuid, sg_set)

        portgroups = self._get_portgroups()
        if dvpg_name in portgroups:
            existing = portgroups[dvpg_name]

            if update:
                self.update_dvportgroup(existing, port_config)
            return existing

        if CONF.AGENT.dry_run:
            return

        try:
            pg_spec = builder.pg_config(port_config)
            pg_spec.name = dvpg_name
            pg_spec.numPorts = 0
            pg_spec.type = 'earlyBinding'
            pg_spec.description = sg_set

            now = timeutils.utcnow()
            pg_create_task = self._dvs.CreateDVPortgroup_Task(spec=pg_spec)

            result = wait_for_task(pg_create_task, si=self.connection)

            pg_ref = result.result

            props = util.get_object_properties_dict(self.connection, pg_ref, ["key"])
            delta = timeutils.utcnow() - now
            stats.timing('networking_dvs.dvportgroup.created', delta)
            LOG.debug("Creating portgroup {} took {} seconds.".format(pg_ref.value, delta.seconds))

            pg = PortGroup(
                key=props["key"],
                ref=pg_ref,
                name=dvpg_name,
                config_version=0,
                description=sg_set,
                default_port_config=port_config
            )
            self._port_groups_by_name[dvpg_name] = pg
            return pg
        except vim.fault.DuplicateName as dn:
            LOG.info("Untagged portgroup with matching name {} found, will update and use.".format(dvpg_name))

            if dvpg_name not in portgroups:
                portgroups = self._get_portgroups(refresh=True)

            if dvpg_name not in portgroups:
                LOG.error("Portgroup with matching name {} not found while expected.".format(dvpg_name))
                return

            existing = portgroups[dvpg_name]

            if update:
                self.update_dvportgroup(existing, port_config)

            return existing
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    @stats.timed()
    def update_dvportgroup(self, pg, port_config=None, name=None, retries=3):
        for ntry in six.moves.xrange(retries):
            pg_ref = pg.ref

            if not pg.name:
                LOG.debug("Missing name for %s", pg_ref.value)

            if pg.async_fetch:
                LOG.warning("Blocking on port-group %s", pg.name)
                pg.async_fetch.wait()

            default_port_config = pg.default_port_config

            if (name == pg.name or not name) \
                    and (default_port_config or not port_config) \
                    and not _config_differs(default_port_config, port_config):
                LOG.debug("Skipping update: No changes to known config on %s", pg.name)
                return

            try:
                pg_spec = builder.pg_config(port_config)
                pg_spec.configVersion = str(pg.config_version)

                if name and name != pg.name:
                    pg_spec.name = name

                now = timeutils.utcnow()
                if not CONF.AGENT.dry_run:
                    pg_update_task = pg_ref.ReconfigureDVPortgroup_Task(spec=pg_spec)
                else:
                    LOG.debug(pg_spec)

                pg.config_version = str(int(pg.config_version) + 1)
                pg.default_port_config = port_config

                if not CONF.AGENT.dry_run:
                    wait_for_task(pg_update_task, si=self.connection)

                delta = timeutils.utcnow() - now
                stats.timing('networking_dvs.dvportgroup.updated', delta)

                LOG.debug("Updating portgroup {} took {} seconds.".format(pg_ref.value, delta.seconds))
                return
            except vim.fault.DvsOperationBulkFault as e:
                self.rectify_for_fault(e)
            except vim.fault.VimFault as e:
                if dvs_const.CONCURRENT_MODIFICATION_TEXT in str(e) \
                        and ntry != retries - 1:
                    LOG.debug("Concurrent modification detected, will retry.")
                    ## TODO A proper read-out of the current config
                    props = util.get_object_properties_dict(self.connection, pg_ref,
                                                                ["config.configVersion", "config.defaultPortConfig"])
                    pg.config_version = props["config.configVersion"]
                    pg.default_port_config = props["config.defaultPortConfig"]

                    continue
                raise exceptions.wrap_wmvare_vim_exception(e)

    def rectify_for_fault(self, fault):
        """
        Handles DvsOperationBulkFault by attempting to rectify the hosts' configuration with the switch
        """
        host_faults = getattr(fault, "hostFault", None)
        hosts = set()
        for hf in host_faults:
            if not hf:
                continue
            host_ref = hf.host
            if host_ref in self.hosts_to_rectify:
                if time.time() - self.rectify_wait > self.hosts_to_rectify[host_ref]:
                    self.hosts_to_rectify[host_ref] = time.time()
                    hosts.add(host_ref)
                else:
                    LOG.debug("Timeout for host {} is not reached yet, skipping.".format(host_ref.value))
            else:
                self.hosts_to_rectify[host_ref] = time.time()
                hosts.add(host_ref)

        if not hosts:
            return
        LOG.debug("Hosts to rectify: {}".format(hosts))
        rectify_task = self.connection.content.dvSwitchManager.RectifyDvsOnHost_Task(hosts=list(hosts))

    def switch_port_blocked_state(self, port):
        try:
            port_info = self.get_port_info(port)
            port_settings = builder.port_setting()
            state = not port['admin_state_up']
            port_settings.blocked = builder.blocked(state)

            update_spec = builder.port_config_spec(
                port_info.config.configVersion, port_settings)
            update_spec.key = port_info.key
            self.update_ports([update_spec])
        except exceptions.PortNotFound:
            LOG.debug("Port %s was not found. Nothing to block." % port['id'])
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def _lookup_unbound_port_or_increase_pg(self, pg):
        while True:
            try:
                port_info = self._lookup_unbound_port(pg)
                break
            except exceptions.UnboundPortNotFound:
                try:
                    self._increase_ports_on_portgroup(pg)
                except (vim.fault.VimFault,
                        exceptions.VMWareDVSException) as e:
                    if dvs_const.CONCURRENT_MODIFICATION_TEXT in e.message:
                        LOG.info(_LI('Concurrent modification on '
                                     'increase port group.'))
                        continue
                    raise e
        return port_info

    def book_port(self, network, port_name, segment, net_name=None):
        try:
            if not net_name:
                net_name = self._get_net_name(self.dvs_name, network)
            pg = self._get_or_create_pg(net_name, network, segment)
            for iter in range(0, 4):
                try:
                    port_info = self._lookup_unbound_port_or_increase_pg(pg)

                    port_settings = builder.port_setting()
                    port_settings.blocked = builder.blocked(False)
                    update_spec = builder.port_config_spec(
                        port_info.config.configVersion, port_settings,
                        name=port_name)
                    update_spec.key = port_info.key
                    update_task = self.submit_update_ports([update_spec])
                    wait_for_task(update_task, si=self.connection)
                    return port_info.key
                except vim.fault.VimFault as e:
                    sleep(0.1)
            raise exceptions.wrap_wmvare_vim_exception(e)
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def release_port(self, port):
        try:
            port_info = self.get_port_info(port)
            update_spec = builder.port_config_spec(
                port_info.config.configVersion, name='')
            update_spec.key = port_info.key
            # setting = builder.port_setting()
            # setting.filterPolicy = builder.filter_policy([])
            # update_spec.setting = setting
            update_spec.operation = 'remove'
            update_task = self.submit_update_ports([update_spec])
            wait_for_task(update_task, si=self.connection)
            self.remove_block(port_info.key)
        except exceptions.PortNotFound:
            LOG.debug("Port %s was not found. Nothing to delete." % port['id'])
        except exceptions.ResourceInUse:
            LOG.debug("Port %s in use. Nothing to delete." % port['id'])
        except vim.fault.VimFault as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def remove_block(self, port_key):
        self._blocked_ports.discard(port_key)

    def _build_pg_create_spec(self, name, vlan_tag, blocked):
        port_setting = builder.port_setting()

        port_setting.vlan = builder.vlan(vlan_tag)
        port_setting.blocked = builder.blocked(blocked)

        port_setting.filterPolicy = builder.filter_policy([])

        pg = builder.pg_config(port_setting)
        pg.name = name
        pg.numPorts = 0

        # Equivalent of vCenter static binding type.
        pg.type = 'earlyBinding'
        pg.description = 'Managed By Neutron'
        return pg

    def _build_dvswitch_update_spec(self):
        dvswitch_config = builder.dv_switch_config()
        return dvswitch_config

    def _build_pg_update_spec(self, config_version,
                              blocked=None,
                              ports_number=None):
        port = builder.port_setting()
        if blocked is not None:
            port.blocked = builder.blocked(blocked)
        pg = builder.pg_config(port)
        if ports_number:
            pg.numPorts = ports_number
        pg.configVersion = config_version
        return pg

    def _get_dvs(self, dvs_name, connection):
        """Get the dvs by name"""

        dvs_list = {}
        with util.WithRetrieval(connection,
                                util.get_objects(connection, vim.DistributedVirtualSwitch, 100, ['name', 'portgroup'])
                                ) as dvswitches:
            for dvs in dvswitches:
                p = {p.name: p.val for p in dvs.propSet}
                if dvs_name == p['name']:
                    return dvs.obj, DVSController._get_datacenter(connection, dvs.obj)
                dvs_list[dvs.obj] = p['portgroup']

        for dvs, port_groups in six.iteritems(dvs_list):
            for pg in port_groups:
                try:
                    name = util.get_object_property(self.connection, pg, 'name')
                    if dvs_name == name:
                        return dvs, DVSController._get_datacenter(connection, dvs)
                except vim.fault.VimFault:
                    pass

        raise exceptions.DVSNotFound(dvs_name=dvs_name)

    @staticmethod
    def _get_datacenter(si, entity_ref, max_objects=100):
        """Get the inventory path of a managed entity.
        :param si: Vim object
        :param entity_ref: managed entity reference
        :return: the datacenter of the entity_ref
        """
        property_collector = si.content.propertyCollector

        prop_spec = util.build_property_spec(vim.Datacenter, ['name'])
        select_set = util.build_selection_spec('ParentTraversalSpec')
        select_set = util.build_traversal_spec('ParentTraversalSpec', vim.ManagedEntity, 'parent', False, [select_set])
        obj_spec = util.build_object_spec(entity_ref, [select_set])
        prop_filter_spec = util.build_property_filter_spec([prop_spec], [obj_spec])
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        options.maxObjects = max_objects
        retrieve_result = property_collector.RetrievePropertiesEx(
            specSet=[prop_filter_spec],
            options=options)

        with util.WithRetrieval(si, retrieve_result) as objects:
            for obj in objects:
                if isinstance(obj.obj, vim.Datacenter):
                    return obj.obj

    def _get_pg_by_name(self, pg_name, refresh_if_missing=True):
        """Get the dpg ref by name"""
        try:
            return self._port_groups_by_name[pg_name]["ref"]
        except KeyError:
            if not refresh_if_missing:
                raise exceptions.PortGroupNotFound(pg_name=pg_name)

            self._get_portgroups(refresh=True)
            try:
                return self._port_groups_by_name[pg_name]["ref"]
            except KeyError:
                raise exceptions.PortGroupNotFound(pg_name=pg_name)

    def _get_all_port_groups(self):
        net_list = util.get_object_property(self.connection, self._datacenter, 'network')
        type_value = vim.DistributedVirtualPortgroup
        return self._get_object_by_type(net_list, type_value)

    def _get_or_create_pg(self, pg_name, network, segment):
        try:
            return self._get_pg_by_name(pg_name)
        except exceptions.PortGroupNotFound:
            LOG.debug(_LI('Network %s is not present in vcenter. '
                          'Perform network creation' % pg_name))
            return self.create_network(network, segment)

    def _get_config_by_ref(self, ref):
        """pg - ManagedObjectReference of Port Group"""
        return util.get_object_property(self.connection, ref, 'config')

    @staticmethod
    def _get_net_name(dvs_name, network):
        # TODO(dbogun): check network['bridge'] generation algorithm our
        # must match it

        return dvs_name + network['id']

    @staticmethod
    def _get_object_by_type(results, type_value):
        """Get object by type.

        Get the desired object from the given objects result by the given type.
        """
        return [obj for obj in results if isinstance(obj, type_value)]

    def _get_ports_for_pg(self, pg_name):
        pg = self._get_pg_by_name(pg_name)
        return util.get_object_property(self.connection, pg, 'portKeys')[0]

    def _get_free_pg_keys(self, port_group):
        criteria = builder.port_criteria(
            port_group_key=port_group.value)
        all_port_keys = set(
            self._dvs.FetchDVPortKeys(criteria=criteria))
        criteria.connected = True
        connected_port_keys = set(self._dvs.FetchDVPortKeys(criteria=criteria))
        return list(all_port_keys - connected_port_keys - self._blocked_ports)

    def _lookup_unbound_port(self, port_group):
        for port_key in self._get_free_pg_keys(port_group):
            self._blocked_ports.add(port_key)
            p_info = self._get_port_info_by_portkey(port_key)
            if not getattr(p_info.config, 'name', None):
                return p_info
        raise exceptions.UnboundPortNotFound()

    def _increase_ports_on_portgroup(self, port_group):
        pg_info = self._get_config_by_ref(port_group)
        # TODO(ekosareva): need to have max size of ports number
        ports_number = max(INIT_PG_PORTS_COUNT, pg_info.numPorts * 2)
        pg_spec = self._build_pg_update_spec(
            pg_info.configVersion, ports_number=ports_number)
        pg_update_task = port_group.ReconfigureDVPortgroup_Task(spec=pg_spec)
        wait_for_task(pg_update_task, si=self.connection)

    def get_port_info(self, port):
        key = port.get('binding:vif_details', {}).get('dvs_port_key')
        if key is not None:
            port_info = self.get_port_info_by_portkey(key)
        else:
            port_info = self._get_port_info_by_name(port['id'])
        return port_info

    def get_port_info_by_portkey(self, port_key):
        """pg - ManagedObjectReference of Port Group"""
        LOG.debug("Fetching port info for {}".format(port_key))
        criteria = builder.port_criteria(port_key=port_key)
        port_info = self._dvs.FetchDVPorts(criteria=criteria)

        if not port_info:
            raise exceptions.PortNotFound(id=port_key)

        if getattr(port_key, '__iter__', None):
            return port_info
        else:
            return port_info[0]

    def _get_port_info_by_name(self, name, port_list=None):
        if port_list is None:
            port_list = self.get_ports(None)
        ports = [port for port in port_list if port.config.name == name]
        if not ports:
            raise exceptions.PortNotFound(id=name)
        if len(ports) > 1:
            LOG.warn(_LW("Multiple ports found for name %s."), name)
        return ports[0]

    def get_ports(self, connect_flag=True):
        criteria = builder.port_criteria(connected=connect_flag)
        ports = self._dvs.FetchDVPorts(criteria=criteria)
        p_ret = []
        for port in ports:
            if (getattr(port.config, 'name', None) is not None and
                    self._valid_uuid(port.config.name)):
                p_ret.append(port)
        return p_ret

    def _get_ports_ids(self):
        return [port.config.name for port in self.get_ports()]

    def _valid_uuid(self, name):
        try:
            uuid.UUID(name, version=4)
        except ValueError:
            return False
        return True


def connect(config, **kwds):
    connection = None
    while not connection:
        try:
            if not config.ca_certs:
                connection = SmartConnectNoSSL(host=config.vsphere_hostname,
                              user=config.vsphere_login,
                              pwd=config.vsphere_password)
            else:
                connection = SmartConnect(host=config.vsphere_hostname,
                              user=config.vsphere_login,
                              pwd=config.vsphere_password)

            if connection:
                atexit.register(Disconnect, connection)
        except ConnectionError:
            LOG.error(_LE("No connection to vSphere"))
            sleep(10)

    return connection


def create_network_map_from_config(config, connection=None, **kwargs):
    """Creates physical network to dvs map from config"""
    connection = connection or connect(config)
    network_map = {}
    for network, dvs in six.iteritems(neutron_utils.parse_mappings(config.network_maps)):
        network_map[network] = DVSController(dvs, connection=connection,
                                             rectify_wait=config.host_rectify_timeout, **kwargs)
    return network_map


def create_port_map(dvs_list, connect_flag=True):
    port_map = {}
    for dvs in dvs_list:
        port_map[dvs] = dict([[port.key, port] for port in dvs.get_ports(connect_flag)])
    return port_map


def get_dvs_and_port_by_id_and_key(dvs_list, port_id, port_key):
    for dvs in dvs_list:
        port = dvs.get_port_info_by_portkey(port_key)
        if port:
            if port.config.name == port_id:
                return dvs, port
    return None, None


def get_dvs_by_id_and_key(dvs_list, port_id, port_key):
    dvs, port = get_dvs_and_port_by_id_and_key(dvs_list, port_id, port_key)
    return dvs
