.PHONY: dev down logs

dev:
	docker compose -f compose.local.yaml up --build

down:
	docker compose -f compose.local.yaml down

logs:
	docker compose -f compose.local.yaml logs -f
