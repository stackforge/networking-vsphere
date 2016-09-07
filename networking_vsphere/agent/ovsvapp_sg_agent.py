# Copyright (c) 2015 Hewlett-Packard Development Company, L.P.
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

import eventlet
from oslo_config import cfg
from oslo_log import log
import oslo_messaging
from oslo_utils import importutils
from pprint import pformat

import netaddr
import six
import threading
import time

from networking_vsphere._i18n import _LI
from networking_vsphere.common import constants as ovsvapp_const

from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.common import rpc as n_rpc

LOG = log.getLogger(__name__)

ovsvapplock = threading.RLock()
sg_datalock = threading.RLock()

ADD_KEY = 'add'
DEL_KEY = 'del'
OVSVAPP_ID = 'OVSVAPP-'
DELETE_TIMEOUT_INTERVAL = 600


class OVSvAppSecurityGroupAgent(sg_rpc.SecurityGroupAgentRpc):
    """OVSvApp derived class from OVSSecurityGroupAgent

    This class is to override the default behavior of some methods.
    """
    def __init__(self, context, ovsvapp_sg_rpc, defer_apply):
        self.context = context
        self.ovsvapp_sg_rpc = ovsvapp_sg_rpc
        self.sgid_rules_dict = {}
        self.sgid_remote_rules_dict = {}
        self.sgid_devices_dict = {}
        self.pending_rules_dict = {}
        self.deleted_devices_dict = {}
        self.init_firewall(defer_apply)
        self.t_pool = eventlet.GreenPool(ovsvapp_const.THREAD_POOL_SIZE)
        LOG.info(_LI("OVSvAppSecurityGroupAgent initialized."))

    @property
    def use_enhanced_rpc(self):
        if self._use_enhanced_rpc is None:
            self._use_enhanced_rpc = False
        return self._use_enhanced_rpc

    def init_firewall(self, defer_refresh_firewall=False):
        firewall_driver = cfg.CONF.SECURITYGROUP.ovsvapp_firewall_driver
        LOG.debug("Init firewall settings (driver=%s).", firewall_driver)
        if not firewall_driver:
            firewall_driver = 'neutron.agent.firewall.NoopFirewallDriver'
        self.firewall = importutils.import_object(firewall_driver)
        # The following flag will be set to true if port filter must not be
        # applied as soon as a rule or membership notification is received.
        self.defer_refresh_firewall = defer_refresh_firewall
        # Stores devices for which firewall should be refreshed when
        # deferred refresh is enabled.
        self.devices_to_refilter = set()
        # Flag raised when a global refresh is needed.
        self.global_refresh_firewall = False
        self._use_enhanced_rpc = None

    def security_groups_provider_updated(self, devices_to_update):
        LOG.info(_LI("Ignoring default security_groups_provider_updated RPC."))

    def sg_provider_updated(self, net_id):
        devices = []
        for device in self.firewall.ports.values():
            if net_id == device.get('network_id'):
                devices.append(device['device'])
                self._remove_device_sg_mapping(device['device'], False)
        if devices:
            LOG.info(_LI("Adding %s devices to the list of devices "
                         "for which firewall needs to be refreshed"),
                     len(devices))
            ovsvapplock.acquire()
            self.devices_to_refilter |= set(devices)
            self.firewall.remove_ports_from_provider_cache(devices)
            ovsvapplock.release()

    def add_devices_to_filter(self, devices):
        if not devices:
            return
        self.firewall.add_ports_to_filter(devices)

    def ovsvapp_sg_update(self, port_with_rules):
        for port_id in port_with_rules:
            if port_id in self.firewall.ports:
                self._update_device_port_sg_map(port_with_rules, port_id)
                self.firewall.prepare_port_filter(port_with_rules[port_id])
        LOG.debug("Port Cache 01: %s",
                  pformat(port_with_rules[port_id]))

    def _expand_rules(self, rules):
        LOG.debug("_expand_rules: %s", pformat(rules))
        rules_list = []
        for rule in rules:
            remote = rule['remote_group_id']
            devices = self.sgid_devices_dict.get(remote)
            if devices is not None:
                for device in devices:
                    new_rule = rule.copy()
                    new_rule.pop('id')
                    direction = rule.get('direction')
                    direction_ip_prefix = (
                        ovsvapp_const.DIRECTION_IP_PREFIX[direction])
                    new_rule[direction_ip_prefix] = str(
                        netaddr.IPNetwork(device).cidr)
                    rules_list.append(new_rule)
        return rules_list

    def _expand_rule_for_device(self, rule, device):
        LOG.debug("_expand_rules_for_device: %s %s", device, rule)
        if device is not None:
            version = netaddr.IPNetwork(device).version
            ethertype = 'IPv%s' % version
            if rule['ethertype'] != ethertype:
                return
            new_rule = rule.copy()
            new_rule.pop('id')
            direction = rule.get('direction')
            direction_ip_prefix = (
                ovsvapp_const.DIRECTION_IP_PREFIX[direction])
            new_rule[direction_ip_prefix] = str(
                netaddr.IPNetwork(device).cidr)
            LOG.debug("_expand_rules_for_device returns: %s", new_rule)
            return new_rule

    def expand_sg_rules(self, ports_info):
        ips = ports_info.get('member_ips')
        ports = ports_info.get('ports')
        for port in ports.values():
            updated_rule = []
            for rule in port.get('sg_normal_rules'):
                remote_group_id = rule.get('remote_group_id')
                direction = rule.get('direction')
                direction_ip_prefix = (
                    ovsvapp_const.DIRECTION_IP_PREFIX[direction])
                if not remote_group_id:
                    updated_rule.append(rule)
                    continue

                port['security_group_source_groups'].append(remote_group_id)
                base_rule = rule
                for ip in ips[remote_group_id]:
                    if ip in port.get('fixed_ips', []):
                        continue
                    ip_rule = base_rule.copy()
                    ip_rule['id'] = OVSVAPP_ID + ip
                    version = netaddr.IPNetwork(ip).version
                    ethertype = 'IPv%s' % version
                    if base_rule['ethertype'] != ethertype:
                        continue
                    ip_rule[direction_ip_prefix] = str(
                        netaddr.IPNetwork(ip).cidr)
                    updated_rule.append(ip_rule)
            port['sg_provider_rules'] = port['security_group_rules']
            port['security_group_rules'] = updated_rule
        return ports

    def _fetch_and_apply_rules(self, dev_ids, update=False):
        ovsvapplock.acquire()
        #  This will help us prevent duplicate processing of same port
        #  when we get back to back updates for same SG or Network.
        self.devices_to_refilter = self.devices_to_refilter - set(dev_ids)
        ovsvapplock.release()
        sg_info = self.ovsvapp_sg_rpc.security_group_info_for_esx_devices(
            self.context, dev_ids)
        time.sleep(0)
        LOG.debug("Successfully serviced security_group_info_for_esx_devices "
                  "RPC for %s.", dev_ids)
        ports = sg_info.get('ports')
        for port_id in ports:
            if port_id in dev_ids:
                port_info = {'member_ips': sg_info.get('member_ips'),
                             'ports': {port_id: ports[port_id]}}
                port_sg_rules = self.expand_sg_rules(port_info)
                if len(port_sg_rules.get(port_id).get(
                       'sg_provider_rules')) == 0:
                    LOG.info(_LI("Missing Provider Rules for port %s"),
                             port_id)
                    self.devices_to_refilter.add(port_id)
                    return

                if self.deleted_devices_dict.get(port_id) is None:
                    self._update_device_port_sg_map(port_sg_rules,
                                                    port_id, update)
                    LOG.debug("Port Cache: %s",
                              pformat(port_sg_rules[port_id]))
#                   if update:
                    if len(port_sg_rules[port_id]['security_group_rules']) > 0 \
                        or \
                       port_sg_rules[port_id].get('security_group_rules_deleted') \
                       is not None:
                        LOG.info(_LI("Applying Changed Rules for Port %s"),
                                 port_id)
                        self.firewall.update_port_filter(
                            port_sg_rules[port_id]
                        )
                    else:
                        LOG.info(_LI("NO RULES CHANGED for Port %s"), port_id)
#                       self.firewall.prepare_port_filter(port_sg_rules[port_id])

    def _process_port_set(self, devices, update=False):
        dev_list = list(devices)
        if len(dev_list) > ovsvapp_const.SG_RPC_BATCH_SIZE:
            sublists = ([dev_list[x:x + ovsvapp_const.SG_RPC_BATCH_SIZE]
                        for x in range(0, len(dev_list),
                                       ovsvapp_const.SG_RPC_BATCH_SIZE)])
        else:
            sublists = [dev_list]
        for dev_ids in sublists:
            self.t_pool.spawn_n(self._fetch_and_apply_rules, dev_ids, update)

    def _print_rules_cache(self, msg):
        LOG.debug("=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=")
        LOG.debug(msg)
        LOG.debug("sgid_devices_dict: %s", pformat(self.sgid_devices_dict))
        LOG.debug("sgid_rules_dict: %s", pformat(self.sgid_rules_dict))
        LOG.debug("sgid_remote_rules_dict: %s",
                  pformat(self.sgid_remote_rules_dict))
        LOG.debug("sgid_pending_rules_dict: %s",
                  pformat(self.pending_rules_dict))
        LOG.debug("=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=")

    def _remove_device_sg_mapping(self, port_id, deleted=True):
        sg_datalock.acquire()
        try:
            LOG.debug("_remove_device_sg_mapping for port: %s", port_id)
            remove_list = []
            deleted_dev = None
            deleted_dev_group = None
            ip = None
            if port_id is not None:
                for group, devices_dict in \
                    six.iteritems(self.sgid_devices_dict):
                    for ip, port in six.iteritems(devices_dict):
                        if port == port_id:
                            deleted_dev = ip
                            deleted_dev_group = group
                            break
                        else:
                            ip = None
                    if ip is not None:
                        value = devices_dict.pop(ip, None)
                        if value is None:
                            LOG.info(_LI("KeyError for %s"), ip)
                            LOG.info(_LI("KeyError devices_dict %(ddict)s,"
                                     "%(deleted_dev)s"),
                                     {'ddict': devices_dict,
                                      'deleted_dev': deleted_dev})
                        if len(devices_dict) == 0:
                            remove_list.append(group)
                if self.pending_rules_dict.get(port_id) is not None:
                    self.pending_rules_dict.pop(port_id)
            LOG.debug("Deleted device ip and group are: %s, %s",
                      deleted_dev, deleted_dev_group)
            for ngroup, rules in six.iteritems(self.sgid_remote_rules_dict):
                if ngroup == deleted_dev_group:
                    continue
                removed_rules = []
                for rule in rules:
                    if rule['remote_group_id'] == deleted_dev_group:
                        ex_rule = self._expand_rule_for_device(rule,
                                                               deleted_dev)
                        if ex_rule is not None:
                            removed_rules.append(ex_rule)
                devs_list = self.sgid_devices_dict.get(ngroup)
                if devs_list is not None:
                    for dev, p_id in six.iteritems(devs_list):
                        prules = self.pending_rules_dict.get(p_id)
                        if prules is None:
                            self.pending_rules_dict[p_id] = prules = {}
                            prules[ADD_KEY] = []
                            prules[DEL_KEY] = []
                        prules[DEL_KEY].extend(removed_rules)
            for port, rules in six.iteritems(self.pending_rules_dict):
                lst = []
                prules = self.pending_rules_dict.get(port)
                if prules[ADD_KEY] is not None:
                    for rule in prules[ADD_KEY]:
                        remote_grp_id = rule.get('remote_group_id')
                        if remote_grp_id is not None:
                            if remote_grp_id == deleted_dev_group:
                                if rule.get('source_ip_prefix') is not None:
                                    if deleted_dev in rule['source_ip_prefix']:
                                        lst.append(rule)
                    if len(lst) > 0:
                        for r in lst:
                            prules[ADD_KEY].remove(r)
            for group in remove_list:
                self.sgid_devices_dict.pop(group)
                self.sgid_rules_dict.pop(group)
                self.sgid_remote_rules_dict.pop(group)
            if deleted:
                self.deleted_devices_dict[port_id] = time.time()
            self._print_rules_cache("After _remove_device_sg_mapping")
        finally:
            sg_datalock.release()

    def _get_ip_rules(self, sgroup_id, device_ip):
        LOG.debug("_get_ip_rules: %s, %s", sgroup_id, device_ip)
        sgid_rules = self.sgid_remote_rules_dict[sgroup_id]
        updated_rules = []
        for rule in sgid_rules:
            remote_group_id = rule.get('remote_group_id')
            if not remote_group_id:
                continue
            direction = rule.get('direction')
            direction_ip_prefix = (
                ovsvapp_const.DIRECTION_IP_PREFIX[direction])
            base_rule = rule
            devices = self.sgid_devices_dict[sgroup_id]
            for device in devices:
                if device == device_ip:
                    ip_rule = base_rule.copy()
                    ip_rule['id'] = None
                    version = netaddr.IPNetwork(device).version
                    ethertype = 'IPv%s' % version
                    if base_rule['ethertype'] != ethertype:
                        continue
                    ip_rule[direction_ip_prefix] = str(
                        netaddr.IPNetwork(device).cidr)
                    updated_rules.append(ip_rule)
        return updated_rules

    def _update_sgid_devices_dict(self, group, sg_devices, port_id):
        new_device = None
        if self.sgid_devices_dict.get(group) is None:
            self.sgid_devices_dict[group] = devices = {}
            for device in sg_devices:
                devices[device] = port_id
                new_device = device
        else:
            devices = self.sgid_devices_dict[group]
            for device in sg_devices:
                if device not in devices:
                    devices[device] = port_id
                    new_device = device
        return new_device

    def _check_and_update_pending_rules(self, group, port_id, added_rules,
                                        deleted_rules, new_arules,
                                        new_drules, remote=False):
        LOG.debug("_check_and_update_pending_rules: %s %s", group, port_id)
        devices = self.sgid_devices_dict[group]
        skip = False
        for device in devices:
            if devices[device] == port_id:
                prules = self.pending_rules_dict.get(port_id)
                if prules is not None:
                    if len(prules[ADD_KEY]) > 0:
                        LOG.debug("Pending rules will be processed(add)")
                        LOG.debug("02.Fol. rules are added for port: %s %s",
                                  port_id, pformat(prules[ADD_KEY]))
                        for r in prules[ADD_KEY]:
                            if r not in added_rules:
                                added_rules.append(r)
                            else:
                                skip = True
                        prules[ADD_KEY] = []
                    if len(prules[DEL_KEY]) > 0:
                        LOG.debug("Pending rules will be processed(delete)")
                        LOG.debug("02.Fol. rules are deleted for port: %s %s",
                                  port_id, pformat(prules[DEL_KEY]))
                        for r in prules[DEL_KEY]:
                            if r not in deleted_rules:
                                deleted_rules.append(r)
                            else:
                                skip = True
                        prules[DEL_KEY] = []
                break
        if not skip:
            for device in devices:
                if devices[device] != port_id:
                    if skip:
                        continue
                    pending_port = devices[device]
                    prules = None
                    if pending_port is not None:
                        prules = self.pending_rules_dict.get(pending_port)
                    if prules is None:
                        self.pending_rules_dict[pending_port] = prules = {}
                        prules[ADD_KEY] = []
                        prules[DEL_KEY] = []
                    if not remote:
                        for r in new_arules:
                            if r not in prules[ADD_KEY]:
                                prules[ADD_KEY].append(r)
                        for r in new_drules:
                            if r not in prules[DEL_KEY]:
                                prules[DEL_KEY].append(r)
                    else:
                        if len(new_arules) > 0:
                            prules[ADD_KEY].extend(
                                self._expand_rules(new_arules))
                        if len(new_drules) > 0:
                            prules[DEL_KEY].extend(
                                self._expand_rules(new_drules))
        self._print_rules_cache("_check_and_update_pending_rules")

    def _update_device_port_sg_map(self, port_info, port_id, update=False):
        sg_datalock.acquire()
        try:
            LOG.info(_LI("_update_device_port_sg_map: %(update)s"
                         " %(port_id)s"),
                     {'update': update, 'port_id': port_id})
            self._print_rules_cache("Before: _update_device_port_sg_map")
            added_rules = []
            deleted_rules = []
            sgroups = port_info[port_id]['security_groups']
            sg_rules = port_info[port_id]['security_group_rules']
            sg_normal_rules = port_info[port_id]['sg_normal_rules']
            sg_devices = port_info[port_id]['fixed_ips']
            for group in sgroups:
                new_rules = []
                rules_map = {}
                new_device = self._update_sgid_devices_dict(
                    group, sg_devices, port_id)
                if new_device is not None:
                    LOG.debug("_update_device_port_sg_map: NEW DEVICE: %s",
                              new_device)
                    added_rules.extend(sg_rules)
                    update = False
                if self.sgid_rules_dict.get(group) is None:
                    LOG.debug("_update_device_port_sg_map: NEW SG %s", group)
                    self.sgid_rules_dict[group] = {}
                    self.sgid_remote_rules_dict[group] = []
                    for rule in sg_rules:
                        if OVSVAPP_ID in rule['id']:
                            continue
                        sgid = rule['security_group_id']
                        if group == sgid:
                            self.sgid_rules_dict[sgid][rule['id']] = rule
                    for rule in sg_normal_rules:
                        if rule.get('remote_group_id') is not None:
                            sgid = rule['security_group_id']
                            if group == sgid:
                                self.sgid_remote_rules_dict[sgid].append(rule)
                else:
                    if new_device is not None:
                        continue
                    update_pending = False
                    rules = self.sgid_rules_dict[group]
                    for rule in sg_rules:
                        if OVSVAPP_ID in rule['id']:
                            ip_device = rule['id'].replace(OVSVAPP_ID, '')
                            sgid = rule['security_group_id']
                            srgid = rule['remote_group_id']
                            if sgid == group and group == srgid:
                                devices = self.sgid_devices_dict[group]
                                if ip_device not in devices:
                                    LOG.debug("_update_device_port_sg_map \
                                        - New member added to our group: %s,\
                                        %s", group, pformat(rule))
                                    new_rules.append(rule)
                            elif sgid == group and srgid != group:
                                devices = self.sgid_devices_dict.get(srgid)
                                if devices is not None:
                                    if ip_device not in devices:
                                        LOG.debug("_update_device_port_sg_map \
                                            - New remote group member added:\
                                            %s", ip_device)
                                        new_rules.append(rule)
                                        update_pending = True
                                else:
                                        LOG.debug("_update_device_port_sg_map\
                                            New First remote group member \
                                            added: %s", ip_device)
                                        new_rules.append(rule)
                            continue
                        sgid = rule['security_group_id']
                        if group == sgid:
                            if rule['id'] not in rules:
                                new_rules.append(rule)
                                LOG.debug("_update_device_port_sg_map - \
                                NEW RULE ADDED SG: %s,\
                                %s", group, pformat(rule))
                                update_pending = True
                            else:
                                rules.pop(rule['id'])
                                rules_map[rule['id']] = rule
                    if len(new_rules) > 0:
                        added_rules.extend(new_rules)
                        LOG.debug("01.Fol. rules are added for port: %s %s",
                                  port_id, pformat(new_rules))
                    if len(rules) > 0:
                        LOG.debug("01.Fol. rules are deleted for port: %s %s",
                                  port_id, pformat(rules))
                        deleted_rules.extend(rules.values())
                        update_pending = True
                    for rule in new_rules:
                        if OVSVAPP_ID not in rule['id']:
                            rules_map[rule['id']] = rule
                    self.sgid_rules_dict[group] = rules_map

                    if update_pending:
                        self._check_and_update_pending_rules(
                            group, port_id, added_rules, deleted_rules,
                            new_rules, rules.values()
                        )
                    if len(rules) > 0 or len(new_rules) > 0:
                        LOG.debug("Foll ports need to be updated with above \
                            rules: %s", pformat(self.pending_rules_dict))
                    new_remote_rules = []
                    remote_rules = []
                    remote_list = []
                    for rule in sg_normal_rules:
                        if rule.get('remote_group_id') is not None:
                            sgid = rule['security_group_id']
                            if group == sgid:
                                remote_rules = \
                                    self.sgid_remote_rules_dict[sgid]
                                if remote_rules is not None:
                                    if rule not in remote_rules:
                                        new_remote_rules.append(rule)
                                    else:
                                        remote_rules.remove(rule)
                                        remote_list.append(rule)
                    if len(new_remote_rules) > 0:
                        LOG.debug("_update_device_port_sg_map:\
                            NEW REMOTE SG RULES ADDED:\
                            %s", pformat(new_remote_rules))
                        added_rules.extend(
                            self._expand_rules(new_remote_rules)
                        )
                    if len(remote_rules) > 0:
                        LOG.debug("_update_device_port_sg_map:\
                            REMOTE SG RULES REMOVED: \
                            %s", pformat(remote_rules))
                        deleted_rules.extend(self._expand_rules(remote_rules))
                    self._check_and_update_pending_rules(
                        group, port_id, added_rules, deleted_rules,
                        new_remote_rules, remote_rules, True
                    )
                    remote_list.extend(new_remote_rules)
                    self.sgid_remote_rules_dict[group] = remote_list
            LOG.debug("_update_device_port_sg_map - \
                Added Rules %s", pformat(added_rules))
            LOG.debug("_update_device_port_sg_map - \
                Removed Rules %s", pformat(deleted_rules))
            self._print_rules_cache("After: _update_device_port_sg_map")
            t1 = time.time()
            del_ports = []
            for port, ptime in six.iteritems(self.deleted_devices_dict):
                if int(t1 - ptime) > DELETE_TIMEOUT_INTERVAL:
                    del_ports.append(port)
            for port in del_ports:
                self.deleted_devices_dict.pop(port)
            port_info[port_id]['security_group_rules'] = added_rules
            if len(deleted_rules) > 0:
                port_info[port_id]['security_group_rules_deleted'] = \
                    deleted_rules
        finally:
            sg_datalock.release()

    def remove_devices_from_sg_map(self, devices):
        sg_datalock.acquire()
        try:
            for group, sg_devices in six.iteritems(self.sgid_devices_dict):
                for device in devices:
                    deleted_dev = None
                    if device in sg_devices.values():
                        for dev, port in six.iteritems(sg_devices):
                            if device == port:
                                deleted_dev = dev
                                break
                    if deleted_dev:
                        sg_devices.pop(deleted_dev)
        finally:
            sg_datalock.release()

    def remove_devices_filter(self, device_id):
        if not device_id:
            return
        self.firewall.clean_port_filters([device_id], True)
        self._remove_device_sg_mapping(device_id)

    def prepare_firewall(self, device_ids):
        """Puts in new rules for input port_ids.

        This routine puts in new rules for the
        input ports shippped as device_ids.

        :param device_ids: set of port_ids for which firewall rules
        need to be created.
        """
        LOG.info(_LI("Prepare firewall rules for %s ports."), len(device_ids))
        self._process_port_set(device_ids)

    def refresh_firewall(self, device_ids=None):
        """Removes all rules for input port_ids and puts in new rules for them.

        This routine erases all rules and puts in new rules for the
        input ports shippped as device_ids.

        :param device_ids: set of port_ids for which firewall rules
        need to be refreshed.
        """
        if not device_ids:
            device_ids = self.firewall.ports.keys()
            if not device_ids:
                LOG.info(_LI("No ports here to refresh firewall."))
                return
        LOG.info(_LI("Refresh firewall rules for %s ports."), len(device_ids))
        self._process_port_set(set(device_ids), True)

    def refresh_port_filters(self, own_devices, other_devices):
        """Update port filters for devices.

        This routine refreshes firewall rules when devices have been
        updated, or when there are changes in security group membership
        or rules.

        :param own_devices: set containing identifiers for devices
        belonging to this ESX host.
        :param other_devices: set containing identifiers for
        devices belonging to other ESX hosts within the Cluster.
        """
        # These data structures are cleared here in order to avoid
        # losing updates occurring during firewall refresh.
        devices_to_refilter = self.devices_to_refilter
        global_refresh_firewall = self.global_refresh_firewall
        self.devices_to_refilter = set()
        self.global_refresh_firewall = False
        LOG.info(_LI("Going to refresh for devices: %s."),
                 len(devices_to_refilter))
        if global_refresh_firewall:
            LOG.info(_LI("Refreshing firewall for all filtered devices."))
#            self.firewall.clean_port_filters(other_devices)
#            self.remove_devices_from_sg_map(other_devices)
            self.refresh_firewall()
        else:
            own_devices = (own_devices & devices_to_refilter)
            other_devices = (other_devices & devices_to_refilter)
#            self.firewall.clean_port_filters(other_devices)
#            self.remove_devices_from_sg_map(other_devices)
            if own_devices:
                LOG.info(_LI("Refreshing firewall for %d own devices."),
                         len(own_devices))
                self.refresh_firewall(own_devices)
            if other_devices:
                LOG.info(_LI("Refreshing firewall for %d other devices."),
                         len(other_devices))
                self.prepare_firewall(other_devices)
        LOG.info(_LI("Finished refresh for devices: %s."),
                 len(devices_to_refilter))


class OVSvAppSecurityGroupServerRpcApi(object):
    """RPC client for security group methods in the plugin."""

    def __init__(self, topic):
        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def security_group_info_for_esx_devices(self, context, devices):
        cctxt = self.client.prepare()
        return cctxt.call(context, 'security_group_info_for_esx_devices',
                          devices=devices)
