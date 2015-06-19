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

from networking_vsphere.tests.scenario import manager

from tempest_lib.common import ssh
from tempest_lib.common.utils import data_utils


class OVSvAppSecurityGroupTestJSON(manager.ESXNetworksTestJSON):

    def _check_connectivity(self, source_ip, dest_ip, should_succeed=True):
        if should_succeed:
            msg = "Timed out waiting for %s to become reachable" % dest_ip
        else:
            msg = "%s is reachable" % dest_ip
        self.assertTrue(self._check_remote_connectivity(source_ip, dest_ip,
                                                        should_succeed), msg)

    def test_port_runtime_update_new_security_group_rule(self):
        """Validate new security group rule update.

        This test verifies the traffic after updating the vm port with new
        security group rule with exsisting security group.
        """
        # Create security group for the server
        group_create_body_update, _ = self._create_security_group()

        # Create server with security group
        name = data_utils.rand_name('server-with-security-group')
        server_id = self._create_server_with_sec_group(
            name, self.network['id'],
            group_create_body_update['security_group']['id'])
        self.addCleanup(self._delete_server, server_id)
        self._fetch_network_segmentid_and_verify_portgroup(self.network['id'])
        device_port = self.client.list_ports(device_id=server_id)
        port_id = device_port['ports'][0]['id']
        floating_ip = self._associate_floating_ips(port_id=port_id)
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=False)

        # Update security group rule for the existing security group
        self.client.create_security_group_rule(
            security_group_id=group_create_body_update['security_group']['id'],
            protocol='icmp',
            direction='ingress',
            ethertype=self.ethertype
        )
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=True)

    def test_port_update_new_security_group(self):
        """This test verifies the traffic after updating.

        the vm port with new security group having appropriate rule.
        """
        # Create security group to update the server
        sg_body, _ = self._create_security_group()
        self.client.create_security_group_rule(
            security_group_id=sg_body['security_group']['id'], protocol='icmp',
            direction='ingress', ethertype=self.ethertype)

        # Create server with default security group
        name = data_utils.rand_name('server-with-default-security-group')
        server_id = self._create_server(name,
                                        self.network['id'])
        self.addCleanup(self._delete_server, server_id)
        self._fetch_network_segmentid_and_verify_portgroup(self.network['id'])
        device_port = self.client.list_ports(device_id=server_id)
        port_id = device_port['ports'][0]['id']
        floating_ip = self._associate_floating_ips(port_id=port_id)
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=False)
        # update port with new security group and check connectivity
        update_body = {"security_groups": [sg_body['security_group']['id']]}
        self.client.update_port(port_id, **update_body)
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=True)

    def test_port_creation_with_multiple_security_group(self):
        """Validate port creation with multiple security group.

        This test verifies the traffic after creating a port with
        multiple security groups.
        """
        # Create security groups
        first_security_group, _ = self._create_security_group()
        second_security_group, _ = self._create_security_group()
        post_body = {
            "name": data_utils.rand_name('port-'),
            "security_groups": [first_security_group['security_group']['id'],
                                second_security_group['security_group']['id']],
            "network_id": self.network['id'],
            "admin_state_up": True}

        # Create port with multiple security group
        body = self.client.create_port(**post_body)
        self.addCleanup(self.client.delete_port, body['port']['id'])
        self.client.create_security_group_rule(
            security_group_id=first_security_group['security_group']['id'],
            protocol='icmp',
            direction='ingress',
            ethertype=self.ethertype)
        self.client.create_security_group_rule(
            security_group_id=second_security_group['security_group']['id'],
            protocol='tcp',
            direction='ingress',
            ethertype=self.ethertype)

        # Create server with given port
        name = data_utils.rand_name('server_with_user_created_port')
        port_id = body['port']['id']

        serverid = self._create_server_user_created_port(
            name, port_id)
        self.addCleanup(self._delete_server, serverid)
        floating_ip = self._associate_floating_ips(
            port_id=port_id)
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=False)
        self._check_public_network_connectivity(
            floating_ip['floatingip']['floating_ip_address'])

    def test_validate_addition_of_ingress_rule(self):
        """test_validate_addition_of_ingress_rule

        This test case is used for validating addition ofingress rule
        """
        # Create security group to update the server
        group_create_body_new, _ = self._create_security_group()
        sg_body, _ = self._create_security_group()
        # Create server with default security group
        name = data_utils.rand_name('server-smoke')
        group_id = group_create_body_new['security_group']['id']
        serverid = self._create_server_with_sec_group(name,
                                                      self.network['id'],
                                                      group_id)
        self.addCleanup(self._delete_server, serverid)
        self._fetch_network_segmentid_and_verify_portgroup(self.network['id'])
        device_port = self.client.list_ports(device_id=serverid)
        port_id = device_port['ports'][0]['id']
        floating_ip = self._associate_floating_ips(port_id=port_id)

        # Now ping the server with the default security group & it should fail.
        self.ping_ip_address(floating_ip['floatingip']['floating_ip_address'],
                             should_succeed=False)
        self._check_public_network_connectivity(
            floating_ip['floatingip']['floating_ip_address'],
            should_connect=False, should_check_floating_ip_status=False)

        protocols = ['icmp', 'tcp']
        for protocol in protocols:
            self.client.create_security_group_rule(
                security_group_id=sg_body['security_group']['id'],
                protocol=protocol,
                direction='ingress',
                ethertype=self.ethertype
            )
        update_body = {"security_groups": [sg_body['security_group']['id']]}
        self.client.update_port(port_id, **update_body)

        # Now ping & SSH to recheck the connectivity & verify.
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=True)
        self._check_public_network_connectivity(
            floating_ip['floatingip']['floating_ip_address'])

    def test_port_update_with_no_security_group(self):
        """Validate port update with no security group.

        This test verifies the traffic after updating the vm port with no
        security group
        """
        # Create security group for the server
        group_create_body_update, _ = self._create_security_group()

        # Create server with security group
        name = data_utils.rand_name('server-with-security-group')
        server_id = self._create_server_with_sec_group(
            name, self.network['id'],
            group_create_body_update['security_group']['id'])
        self.addCleanup(self._delete_server, server_id)
        self._fetch_network_segmentid_and_verify_portgroup(self.network['id'])
        device_port = self.client.list_ports(device_id=server_id)
        port_id = device_port['ports'][0]['id']
        floating_ip = self._associate_floating_ips(port_id=port_id)

        # Update security group rule for the existing security group
        self.client.create_security_group_rule(
            security_group_id=group_create_body_update['security_group']['id'],
            protocol='icmp',
            direction='ingress',
            ethertype=self.ethertype
        )
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=True)
        self.client.update_port(port_id, security_groups=[])
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=False)

    def test_security_group_rule_with_default_security_group_id(self):
        """Validate security group rule with default security group id.

        This test verifies the traffic after updating the default security
        group with a new security group rule.
        """
        # Create server with default security group.
        name = data_utils.rand_name('server-with-security-group')
        server_id = self._create_server(
            name, self.network['id'])
        self.addCleanup(self._delete_server, server_id)
        self._fetch_network_segmentid_and_verify_portgroup(self.network['id'])
        device_port = self.client.list_ports(device_id=server_id)
        port_id = device_port['ports'][0]['id']
        sec_grp_id = device_port['ports'][0]['security_groups'][0]
        floating_ip = self._associate_floating_ips(port_id=port_id)
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=False)

        # Update security group rule for the default security group.
        self.client.create_security_group_rule(
            security_group_id=sec_grp_id,
            protocol='icmp',
            direction='ingress',
            ethertype=self.ethertype
        )
        self.ping_ip_address(
            floating_ip['floatingip']['floating_ip_address'],
            should_succeed=True)

    def test_validate_addition_of_icmp_sec_rules(self):

        """Validate  the addition of SSH & ICMP rules.

        1.  Create a custom security group.
        2.  Add the rules(icmp) to the custom security group
        3.  Boot VM with custom security rule.
        4.  Validate the security group rule is applied to the port.
        5.  Validate the vlan-id in the PG is binded with segment id.
        6.  Check public connectivity after associating the floating ip
            like ping & ssh to the VM.
        """
        ser_one, ser_two, c = self._create_multiple_server_on_different_host()
        if c is True:
            device_port1 = self.client.list_ports(device_id=ser_one)
            port_id1 = device_port1['ports'][0]['id']
            sg_first = device_port1['ports'][0]['security_groups'][0]
            device_port2 = self.client.list_ports(device_id=ser_two)
            floating_ip = self._associate_floating_ips(port_id=port_id1)
            fip = floating_ip['floatingip']['floating_ip_address']
            sg_second = device_port2['ports'][0]['security_groups'][0]
            ip = device_port2['ports'][0]['fixed_ips'][0]['ip_address']
            self.client.create_security_group_rule(
                security_group_id=sg_first,
                protocol='tcp',
                direction='ingress',
                ethertype=self.ethertype
            )
            self.ping_ip_address(fip, should_succeed=False)
        # Create security group to update the server1 & check for icmp
            self.client.create_security_group_rule(
                security_group_id=sg_second,
                protocol='icmp',
                direction='ingress',
                ethertype=self.ethertype
            )

        # Now check for the 1st server with the ssh
            self.sh_source = ssh.Client(
                floating_ip['floatingip']['floating_ip_address'], "cirros",
                "cubswin:)")
            cmd = 'ping ' + ip + '  -c 4 > out.log'
            cmd += ' kill -9 `pidof "ping"` || true'
            self.sh_source.exec_command(cmd)
            self.sh_source.exec_command('exit')

        else:
            pass

    def test_security_group_rule_with_remote_sg(self):
        """Validate security group rule with remote security group.

        This test verifies the traffic after adding the remote
        security group rule with exsisting security group.
        """
        # Create two server with different security group.
        ser_one, ser_two, c = self._create_multiple_server_on_different_host()

        if c is True:
            device_port1 = self.client.list_ports(device_id=ser_one)
            sg_first = device_port1['ports'][0]['security_groups'][0]

            device_port2 = self.client.list_ports(device_id=ser_two)
            port_id2 = device_port2['ports'][0]['id']
            floating_ip = self._associate_floating_ips(port_id=port_id2)
            fip = floating_ip['floatingip']['floating_ip_address']
            sg_second = device_port2['ports'][0]['security_groups'][0]
            ip = device_port1['ports'][0]['fixed_ips'][0]['ip_address']

            # Add icmp rule for first server security group.
            self.client.create_security_group_rule(
                security_group_id=sg_second,
                protocol='icmp',
                direction='ingress',
                ethertype=self.ethertype
            )
            # Add tcp rule to ssh to first server.
            self.client.create_security_group_rule(
                security_group_id=sg_second,
                protocol='tcp',
                direction='ingress',
                ethertype=self.ethertype
            )
            # Add group id of first sg to second sg.
            self.client.create_security_group_rule(
                security_group_id=sg_first,
                direction='ingress',
                remote_group_id=sg_second,
                ethertype=self.ethertype
            )
            # Ping second server from first server.
            val = self._check_remote_connectivity(fip, ip)
            if val is False:
                break

        else:
            pass
