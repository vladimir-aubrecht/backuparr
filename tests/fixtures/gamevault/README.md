These are genuine PostgreSQL 17 tar dumps, generated with `postgres:17-alpine`
(image digest `sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`).
They contain only synthetic test data, never real credentials or user data.

1. Create `source` and empty `target` databases.
2. In `source`, create `gamevault_user` (serial id, username, API key, password),
   `progress` (serial id, state, minutes, timestamp), and `strange table` (quoted
   column, bytea, nullable text). Insert synthetic text, UTF-8, escaped tab/newline,
   NULL, empty-string and bytea values.
3. `pg_dump -U postgres -F t source > source.tar`
4. `pg_restore -U postgres --exit-on-error -O -F t -d target < source.tar`
5. `pg_dump -U postgres -F t target > target.tar`
6. Compress with gzip (mtime=0) for storage. Tests decompress locally, never execute
   the contained SQL, and compare the driver's logical fingerprints.

The real database restore and comparison completed successfully. These fixtures
verify the actual native dump format used by GameVault's database service; they
are not a substitute for an end-to-end test against a running GameVault server.
