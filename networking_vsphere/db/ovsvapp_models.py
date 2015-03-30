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

import sqlalchemy as sa

from neutron.db import model_base


class ClusterVNIAllocations(model_base.BASEV2):
    """Represents a VXLAN Network to Local VLAN binding in a cluster."""

    __tablename__ = "ovsvapp_cluster_vni_allocations"

    vcenter_id = sa.Column(sa.String(36), nullable=False, primary_key=True)
    cluster_id = sa.Column(sa.String(1024), nullable=False, primary_key=True)
    lvid = sa.Column(sa.Integer, nullable=False, autoincrement=False,
                     primary_key=True)
    network_id = sa.Column(sa.String(36), default=None)
    allocated = sa.Column(sa.Boolean, nullable=False, default=False)
    network_port_count = sa.Column(sa.Integer, nullable=False, default=0)
    pending_release = sa.Column(sa.Boolean, nullable=False, default=False)

    def __repr__(self):
        """Cluster VNI allocations representation."""
        return ("<ClusterVNIAllocation(%s,%s,%s,%s,%s,%s, %s)>." %
                (self.vcenter_id, self.cluster_id, self.lvid, self.network_id,
                 self.allocated, self.network_port_count,
                 self.pending_release))

    def __eq__(self, other):
        """Compare only the allocation."""
        return (self.vcenter_id == other.vcenter_id and
                self.cluster_id == other.cluster_id and
                self.lvid == other.lvid and
                self.network_id == other.network_id and
                self.allocated == other.allocated and
                self.network_port_count == other.network_port_count and
                self.pending_release == other.pending_release)
