with open("tests/backend/test_security_error_handling.py", "r") as f:
    content = f.read()

content = content.replace('with patch("agents.coordinator_agent.CoordinatorAgent") as mock_coordinator:', 'with patch("agents.orchestrator_agent.OrchestratorAgent") as mock_coordinator:')
content = content.replace('mock_instance.process_query.side_effect', 'mock_instance.run.side_effect')

with open("tests/backend/test_security_error_handling.py", "w") as f:
    f.write(content)
