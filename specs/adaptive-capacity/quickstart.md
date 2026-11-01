# Quickstart

1. Run `uv run pytest tests/fleet/test_error_budget.py tests/fleet/test_throttle_controller_served.py tests/api/test_capacity_served.py`.
2. Call `capacity.throttle.status` through the served `find` operation with a `capacity:read` principal and confirm the controller mode and limits.
3. Call `capacity.throttle.set_mode` with a `capacity:admin` principal and confirm observe versus enforce changes the admission limit.
