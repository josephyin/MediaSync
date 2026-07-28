from __future__ import annotations

import json

from app.appliance.health import (
    REQUIRED_COMPONENTS,
    HealthCheckError,
    collect_health_status,
    is_healthy,
)


def main() -> None:
    try:
        status = collect_health_status()
    except HealthCheckError as exc:
        status = {name: False for name in REQUIRED_COMPONENTS}
        print(
            json.dumps(
                {**status, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from None

    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if is_healthy(status) else 1)


if __name__ == "__main__":
    main()
