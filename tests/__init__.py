"""Unit test suite for :mod:`kolla_otel`.

Laid out OpenStack-style: one ``test_<module>.py`` per source module, and
within each file the tests are grouped into ``<Unit>TestCase`` classes — one
class per unit under test (a class, or a module's related free functions),
with every public method/function covered by at least one ``test_*`` method.

The suite stays on pytest (fixtures, ``parametrize``, ``pytest.raises``);
``python_classes`` in ``pyproject.toml`` teaches the collector to pick up the
``*TestCase`` names. The ``cliff`` command's playbook execution is exercised
with an in-memory double for ``run_playbooks`` so no real Ansible run is
required.
"""
