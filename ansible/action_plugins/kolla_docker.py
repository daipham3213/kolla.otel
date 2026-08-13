# Copyright 2024 kolla-otel
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Backward-compat companion for the ``kolla_container`` action plugin.

kolla-ansible 2023.1 and earlier drive containers through the ``kolla_docker``
module/action rather than ``kolla_container`` (renamed when podman support and
the container-engine worker abstraction landed). Ansible binds an action plugin
to a task by filename, so intercepting ``kolla_docker`` tasks needs a file of
that name — but the logic is identical, so this shim simply re-exports the
sibling ``kolla_container`` plugin's :class:`ActionModule` verbatim. That class
delegates to whichever module name it was invoked as (``self._task.action``),
i.e. ``kolla_docker`` here, so a single implementation serves both releases.

The sibling is loaded by path (not ``import``) because Ansible does not put the
action-plugin directory on ``sys.path``; the exec depends only on the standard
library and the sibling's own imports (ansible + stdlib), so — like the
sibling — it never fails to load merely because ``kolla_otel`` is absent,
preserving the fail-open contract.
"""

import importlib.util
import os

_SIBLING = os.path.join(os.path.dirname(__file__), "kolla_container.py")
_spec = importlib.util.spec_from_file_location(
    "kolla_otel_kolla_container_action_impl", _SIBLING
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

# Re-export so Ansible finds ``ActionModule`` for ``kolla_docker`` tasks.
ActionModule = _module.ActionModule
