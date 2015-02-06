# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
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


from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.openstack.common import log as logging
import time

LOG = logging.getLogger(__name__)


class OVSVAppSecurityGroupAgent(sg_rpc.SecurityGroupAgentRpc):
    """OVSvApp derived class from OVSSecurityGroupAgent

    This class is to override the default behavior of some methods.
    """
    def __init__(self, context, plugin_rpc, root_helper, defer_apply):
        self.context = context
        self.plugin_rpc = plugin_rpc
        self.root_helper = root_helper
        self.init_firewall(defer_apply)
        LOG.info(_("OVSVAppSecurityGroupAgent initialized"))

    def add_devices_to_filter(self, devices):
        if not devices:
            return
        self.firewall.add_ports_to_filter(devices)

    def ovsvapp_sg_update(self, port_with_rules):
        for port_id in port_with_rules:
            if port_id in self.firewall.ports:
                self.firewall.prepare_port_filter(port_with_rules[port_id])

    def remove_device_filters(self, device_id):
        if not device_id:
            return
        LOG.info(_("Remove device filters for %r"), device_id)
        self.firewall.clean_port_filters([device_id], True)

    def prepare_firewall(self, device_ids):
        LOG.info(_("Prepare firewall rules %s"), len(device_ids))
        dev_list = list(device_ids)
        if len(dev_list) > 10:
            sublists = [dev_list[x:x + 10] for x in xrange(0, len(dev_list),
                                                           10)]
        else:
            sublists = [dev_list]
        for dev_ids in sublists:
            devices = self.plugin_rpc.security_group_rules_for_devices(
                self.context, dev_ids)
            for device in devices.values():
                if device['id'] in dev_ids:
                    self.firewall.prepare_port_filter(device)

    def refresh_firewall(self, device_ids=None):
        LOG.info(_("Refresh firewall rules"))
        if not device_ids:
            device_ids = self.firewall.ports.keys()
            if not device_ids:
                LOG.info(_("No ports here to refresh firewall"))
                return
        dev_list = list(device_ids)
        if len(dev_list) > 10:
            sublists = [dev_list[x:x + 10] for x in xrange(0, len(dev_list),
                                                           10)]
        else:
            sublists = [dev_list]

        for dev_ids in sublists:
            # Sleep is to prevent any device_create calls from getting starved
            time.sleep(0)
            devices = self.plugin_rpc.security_group_rules_for_devices(
                self.context, dev_ids)
            for device in devices.values():
                if device['id'] in dev_ids:
                    self.firewall.update_port_filter(device)

    def refresh_port_filters(self, own_devices, other_devices):
        """Update port filters for devices.

        This routine refreshes firewall rules when devices have been
        updated, or when there are changes in security group membership
         or rules.

        :param own_devices: set containing identifiers for devices
        belonging to this ESX host
        :param other_devices: set containing identifiers for
        devices belonging to other ESX hosts within the Cluster
        """
        # These data structures are cleared here in order to avoid
        # losing updates occurring during firewall refresh
        devices_to_refilter = self.devices_to_refilter
        global_refresh_firewall = self.global_refresh_firewall
        self.devices_to_refilter = set()
        self.global_refresh_firewall = False
        LOG.info(_("Going to refresh for devices: %s")
                 % devices_to_refilter)
        if global_refresh_firewall:
            LOG.debug("Refreshing firewall for all filtered devices")
            self.firewall.clean_port_filters(other_devices)
            self.refresh_firewall()
        else:
            own_devices = (own_devices & devices_to_refilter)
            other_devices = (other_devices & devices_to_refilter)
            self.firewall.clean_port_filters(other_devices)
            if own_devices:
                LOG.info(_("Refreshing firewall for %d devices")
                         % len(own_devices))
                self.refresh_firewall(own_devices)
            if other_devices:
                LOG.info(_("Refreshing firewall for %d devices")
                         % len(other_devices))
                self.prepare_firewall(other_devices)
