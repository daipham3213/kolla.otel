"""Test suite for :mod:`kolla_otel`.

One module per source module, covering the configuration model, its
translation into Ansible ``otel_*`` variables, and the ``cliff`` command.
The command's playbook execution is exercised with an in-memory double for
``run_playbooks`` so no real Ansible run is required.
"""
