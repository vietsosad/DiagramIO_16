# DiagramIo Project Guidelines

## Tech Stack

- Use Java 21 and Spring Boot for the backend REST API.
- Use PostgreSQL as the primary database.
- Use Spring Data JPA with Hibernate for persistence and Flyway for versioned database migrations.
- Use Spring Security for authentication and authorization. Implement short-lived JWT access tokens, refresh-token rotation/revocation, and BCrypt or Argon2 password hashing; do not store refresh tokens in plaintext.
- Use Jakarta Bean Validation (Hibernate Validator) for request validation and Jackson for JSON serialization/deserialization.
- Use JUnit 5, Mockito, and Spring Boot Test/MockMvc for automated tests. Use Testcontainers with PostgreSQL for tests that require database behavior when practical.
- Use springdoc-openapi for OpenAPI/Swagger documentation.
- Use Maven as the default build and dependency-management tool unless the repository already establishes Gradle.
- Package and run the service with Docker and Docker Compose. Keep local infrastructure configuration reproducible and do not commit secrets.
- Keep the Docker image layered (dependencies apart from application classes), running as a non-root user, with the container-aware JVM flags in `JAVA_TOOL_OPTIONS`. Keep `mvnw` and every `*.sh` executable in git (mode `100755`).

## Architecture

- Phase 1 is a baseline: prefer the simplest approach that works. Do not add complex logic, premature optimization, or extra abstractions.
- Use three layers: API -> Business (domain) -> Data access (infrastructure). Dependencies point inward only.

| Layer | Package | May use |
|---|---|---|
| API | `com.diagramio.api` | Spring Web: controllers, DTOs, `@RestControllerAdvice` that maps domain exceptions to HTTP status codes |
| Business | `com.diagramio.domain` | Plain Java only: domain models, services, repository interfaces, domain exceptions |
| Data access | `com.diagramio.infrastructure` | Spring Data JPA (`@Entity`, `JpaRepository`) or hand-written SQL (`NamedParameterJdbcTemplate`), classes implementing the domain repository interfaces, Entity <-> domain mappers |
| Wiring | `com.diagramio.config` | Spring configuration: security, OpenAPI, `@Bean` definitions that create domain services |

- The business layer must NOT import any web framework or database library (no Spring, `jakarta.persistence`, Hibernate, Servlet). No `@Service`, `@Transactional`, `@Entity`, or `ResponseEntity` in `domain`. Register domain services as beans in `config`.
- The API layer calls domain services only, never `infrastructure` directly.
- `ArchitectureTest` enforces these rules; keep it green.
- Authentication is done once in a security filter (JWT or `X-Share-Token`), not repeated in each endpoint.
- Controllers receive the caller as a method parameter, resolved by `CallerArgumentResolver`: `Caller caller` for diagram, version and public endpoints (anonymous when there is no login or link), or `@CurrentUser UUID userId` for endpoints that need an account (401 otherwise). Never read `Authentication` or use `@AuthenticationPrincipal` in a controller.
- Services check diagram permissions only through `DiagramAccess` (`VIEW`, `VIEW_HISTORY`, `EDIT`, `MANAGE`): 404 if missing, 401 if the caller has neither an account nor a link to that diagram, 403 otherwise. Do not write `caller.canX(...)` checks or throw access exceptions by hand in services.
  - `require(diagramId, caller, Permission)` reads only `DiagramAccessInfo` (owner, public flag, version), never `data`. Use it whenever the operation does not return the diagram content.
  - `requireLocked(...)` does the same with `SELECT ... FOR UPDATE`; call it inside `TransactionRunner` when concurrent requests must queue (save or restore a version).
  - `requireDiagram(...)` loads the whole diagram, only when the response returns its content.

## Database Migrations

- Flyway migrations live in `src/main/resources/db/migration`, named `V<number>__<description>.sql` (two underscores).
- Never edit a migration that has been merged to `main`; add a new migration instead (Flyway verifies checksums and fails on changed files).
- Hibernate runs with `ddl-auto: validate`; the schema is owned by Flyway only.
- Tables used by several members are defined in `V1`. Each member adds changes for their own tables in a new migration.

## Project Scope

- Build the backend REST API for an online UML and ERD diagram service.
- Keep the current phase focused on account management, diagram storage, access control, sharing, version history, and restoration.
- Do not implement frontend/canvas editing, real-time collaboration through WebSocket/CRDT/OT, third-party OAuth, or email password recovery.
- Treat diagram export to PNG, SVG, PDF, and JSON as a Phase 2 capability. Do not add export endpoints to the current phase unless the user explicitly requests a scope change.

## API Contract

- Use the `/api/v1` prefix for all application endpoints.
- Preserve the endpoint paths, HTTP methods, authentication requirements, and owner/editor/viewer permissions defined in `private/BTL KTPM.md`.
- Return JSON responses with clear, consistent error details and appropriate HTTP status codes.
- Public access is limited to public diagrams, valid sharing tokens, and the health endpoint. Protect all other resources with authentication.
- A newly created diagram must belong to the authenticated user, accept only `UML` or `ERD`, initialize its data as `{ "nodes": [], "edges": [] }`, and be private by default.

## Security and Authorization

- Never store plaintext passwords. Use the project's established password-hashing and token-handling libraries.
- Validate authentication and resource authorization at the service boundary; do not rely on frontend checks.
- Only owners may create, modify, or revoke sharing entries. Editors may update diagrams and create or restore versions but may not manage sharing.
- Generate cryptographically secure, unique sharing tokens and invalidate them immediately when revoked.
- Do not expose private diagram data, credentials, refresh tokens, or internal implementation details in public responses.
- Spring Boot Actuator serves only `health` and `prometheus`, only on the separate management port (`MANAGEMENT_PORT`, default 9090) bound to `127.0.0.1`. Never expose Actuator on the API port; `ManagementEndpointsE2eTest` guards this.
- Validate and constrain request bodies, including the maximum 2 MB diagram JSON payload, before persistence.
- `RequestSizeLimitFilter` rejects bodies above `diagramio.http.max-request-size` (default 4 MB) with 413 before anything reads or parses them (ADR-0005). Keep it ahead of Spring Security; the 2 MB `DiagramData` rule stays the domain check (400).
- Diagram JSON is a `DiagramData` value object: build it with `DiagramData.of(json)` from user input (validates the `nodes`/`edges` shape and 2 MB) and with `DiagramData.fromStorage(json)` only for data read back from the database. Do not pass raw JSON strings through the domain.

## Multiple Instances and Shutdown

- Several instances run behind Nginx (ADR-0006). The Nginx configuration lives only in `deploy/nginx/` (`http.conf`, `server.conf`); `docker-compose.scale.yml`, the Kaggle bench (`bench/kaggle/env.sh`, `lb_config`) and `NginxFailoverE2eTest` all use these files. Change it there, and CI runs `nginx -t` on it.
- Nginx must never send a POST or PATCH again once it reached an instance: do not add `non_idempotent` to `proxy_next_upstream`. POST and PATCH use the upstream without keepalive (`diagramio_fresh`), so an instance that is shutting down refuses them at connect time and they move on safely.
- More than one instance needs cache mode `two-level` (or `redis`); `local` would serve stale data across instances. Keep the total number of database connections constant when changing the instance count (`APP_POOL_SIZE` = total / instances; bench `POOL_TOTAL`).
- Readiness (`/actuator/health/readiness`) reports only the instance itself. Never add PostgreSQL, Redis or another shared dependency to it: one outage would take every instance out of the load balancer at once.
- Keep the shutdown order (SmartLifecycle phases, higher stops first): readiness `REFUSING_TRAFFIC`, `ShutdownConfig` closes the listening socket (`WebServerGracefulShutdownLifecycle.SMART_LIFECYCLE_PHASE + 1`), Spring Boot drains running requests (`SHUTDOWN_TIMEOUT`), then beans that requests still need stop (`RedisCacheMonitor` at `SMART_LIFECYCLE_PHASE - 2048`). A new lifecycle bean that serves requests must use a phase below the web server's. `GracefulShutdownE2eTest` guards this.
- A planned restart first takes the instance out of Nginx (`down`, reload, wait until no worker of the old configuration is left), then stops it: `deploy/rolling-restart.sh`. `nginx -s reload` returns before old workers exit; never assume it is done.

## Data Integrity and Concurrency

- Enforce optimistic locking for diagram updates using the client-supplied `version`.
- Reject stale updates with `409 Conflict` and leave the stored diagram unchanged.
- Treat saved diagram versions as immutable snapshots.
- Before restoring an old version, snapshot the current state, then copy the selected snapshot into the current diagram state atomically.
- Keep authorization checks, version checks, and related writes within the appropriate transaction boundaries.
- Read only the columns an operation needs: lists and version history use summary projections without `data`; snapshots and restores copy `data` inside the database (`INSERT ... SELECT`, `UPDATE ... FROM`) instead of sending it through the app.
- Do not mix JPA and hand-written SQL on the same table: inside one transaction a JPA entity already loaded does not see JDBC `UPDATE`/`DELETE`. The `diagrams`, `diagram_versions` adapters are SQL only.
- Diagrams and versions are cached (`infrastructure/cache`, ADR-0003; mode `diagramio.cache.mode`: `none`, `local`, `redis`, `two-level`). Keep its invariants:
  - Every write to `diagrams` goes through `DiagramRepository`, so the decorator can write the new state to the cache after commit. Never change that table from another adapter or with SQL elsewhere.
  - The cache only accepts a newer `version` (version-checked write-through); deletes leave a tombstone. Do not add plain "put" or "evict then reload" paths.
  - Removals leave a version floor, never a hole (ADR-0003 §8): when versions below `v` are known stale (own write, change from another instance, failed conditional UPDATE), raise the floor before dropping entries, write L1 only through `putIfNewer` (floor and epoch checked inside the key's `compute`), and on Redis replace the key with a floor marker instead of `DEL`. Dropping the whole L1 bumps the epoch first.
  - A failed Redis command turns off L2 only; L1 is dropped only when the pub/sub heartbeat is lost. A failure of a command sent before the last state change must not change the state.
  - Cache entries are immutable (`CachedDiagram`); callers always get a new `Diagram`.
  - A conditional GET checks access before answering 304; never return 304 to a caller without the permission.
  - Access info read for a check may lag behind the database (cache, another instance) but never leads it: reject a PUT early only when its version is lower; let the conditional UPDATE decide everything else, and never write a column (such as `is_public`) from that read.
- Share-token lookups are cached too (ADR-0004; `diagramio.cache.share-tokens`): in JVM for `local`, only in Redis for `redis`/`two-level`. Keep its invariants:
  - Cache keys are the SHA-256 of the token; never store a raw token in a cache.
  - Every change to a link (role, revocation) goes through `DiagramShareRepository`, which marks the link as changing BEFORE the database change and drops the key after commit. If the marker cannot be written, do not change the link (503). Never add a path that changes `diagram_shares` without the marker.
  - The change marker must live at least as long as a cached positive entry; lookups fall back to the database when the cache fails, never allow a token that could not be checked.
- Use transactions by rule, not everywhere. Domain services get them through the `TransactionRunner` port:

| Operation | Transaction | Example |
|---|---|---|
| Read, check, then write | Yes (`TransactionRunner`) | restore a version, rotate a refresh token |
| Several rows or tables that must stay consistent | Yes | restore = backup snapshot + overwrite diagram |
| Concurrent requests must queue | Yes, plus a row lock (`SELECT ... FOR UPDATE`) | save or restore a version |
| One write that is already safe on its own | No | delete a share; update a diagram (one `UPDATE ... WHERE version = :expected`); change a share role (one conditional `UPDATE`) |
| Pure reads | No | get a diagram, list versions |
| Calls to outside systems (email, HTTP, files) | Never inside a transaction | do them after commit |

- Keep transactions short: each one holds a pooled connection and any row locks until it ends.

## Implementation Practices

- Inspect the existing repository structure, framework conventions, schemas, and error-handling patterns before adding code.
- Reuse existing helpers, middleware, validators, and response types instead of duplicating them.
- Keep controllers/routes thin; place business rules in the appropriate service/domain layer.
- Validate enum values, identifiers, ownership, share permissions, token state, and JSON schema at the API boundary.
- Make migrations and schema changes explicit, reversible where the project supports it, and consistent with existing naming conventions.
- Update OpenAPI/Swagger documentation whenever an endpoint or request/response contract changes.
- Do not introduce unrelated refactors or silently change existing behavior.

## Testing and Verification

- Add or update tests for authentication, authorization, public/private access, sharing-token revocation, invalid payloads, optimistic-lock conflicts, immutable versions, and safe restoration.
- Include both successful and denied paths for owner, editor, viewer, unauthenticated, and public requests.
- Verify status codes and response shapes, not only whether a request succeeds.
- `SqlPerRequestE2eTest` pins the SQL statements, commits, pool checkouts, and `data` round trips of every endpoint (with the cache off, `diagramio.cache.mode=none`). A change that alters them must update the expected numbers in the same PR; performance claims are measured with `bench/` (see `docs/adr/0001-phuong-phap-do.md`).
- Run the smallest relevant formatter, linter, type checker, and test commands before considering a change complete.
- CI (`.github/workflows/ci.yml`) runs `./mvnw verify`, `shellcheck` on the bench scripts, a Python syntax check and a Docker build on every pull request and push to `main`; keep it green.
- Use the repository's documented commands and configuration; do not invent replacement tooling without a clear need.

## Documentation and Delivery

- Keep API documentation and implementation behavior synchronized.
- Document intentional scope changes, especially any decision to move a Phase 2 feature into the current phase.
- Report validation commands and any unresolved environment limitations with the completed change.
