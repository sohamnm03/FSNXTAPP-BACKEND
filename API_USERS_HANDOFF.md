# Users API Handoff

## Get users

**Method and API**

```http
GET /api/users
```

**Request payload**

No request payload.

**Response - 200 OK**

```json
{
  "success": true,
  "users": [
    {
      "username": "john.doe",
      "email": "john.doe@example.com",
      "full_name": "John Doe",
      "isActive": 1,
      "isDev": 1,
      "isTesting": 0,
      "isConfig": 1,
      "isFsNxt": 1,
      "isSapFsNxt": 0,
      "modules": ["development"],
      "created_at": "2026-10-13T10:30:00",
      "updated_at": "2026-10-13T11:45:00"
    }
  ]
}
```

## Create or update user

**Method and API**

```http
POST /api/users
```

**Request payload**

```json
{
  "username": "john.doe",
  "email": "john.doe@example.com",
  "full_name": "John Doe",
  "isActive": true,
  "isDev": true,
  "isTesting": false,
  "isConfig": true,
  "isFsNxt": true,
  "isSapFsNxt": false
}
```

**Create response - 201 Created**

```json
{
  "success": true,
  "message": "User created successfully.",
  "user": {
    "id": 42,
    "username": "john.doe",
    "email": "john.doe@example.com",
    "full_name": "John Doe",
    "isActive": 1,
    "isDev": 1,
    "isTesting": 0,
    "isConfig": 1,
    "isFsNxt": 1,
    "isSapFsNxt": 0
  }
}
```

**Update response - 200 OK**

```json
{
  "success": true,
  "message": "User updated successfully.",
  "user": {
    "id": 42,
    "username": "john.doe",
    "email": "john.doe@example.com",
    "full_name": "John Doe",
    "isActive": 1,
    "isDev": 1,
    "isTesting": 0,
    "isConfig": 1,
    "isFsNxt": 1,
    "isSapFsNxt": 0
  }
}
```

## Delete user

**Method and API**

```http
DELETE /api/users
```

**Request payload**

```json
{
  "email": "john.doe@example.com"
}
```

**Response - 200 OK**

```json
{
  "success": true,
  "message": "User deleted successfully.",
  "email": "john.doe@example.com"
}
```

## Access (FS Sprint passes)

- Users now carry `isAdmin` (1/0) in `GET /api/users`; an email in the `ADMIN_EMAILS` setting is
  reported as `isAdmin: 1`.
- `POST /api/users` accepts optional `isAdmin`, `isMobile` and `isDeploy`; a flag left out keeps
  its stored value (0 for a new user). `isAdmin` is only stored from an admin pass.
- `POST`/`DELETE /api/users` and `POST /api/versions` always need an admin pass
  (`Authorization: Bearer <pass>`): 401 without a valid pass, 403 for a non-admin pass.
- Reads: a request with a Bearer pass is always checked (401 invalid/expired). Without a pass,
  `GET /api/users` works only while the `LEGACY_OPEN_READS` setting is on (default; the installed
  0.3.22 reads it anonymously). With `LEGACY_OPEN_READS=0` it needs any valid pass.
  `GET /api/versions` stays open (the sign-in screen reads it).
- Version ends: `POST /api/versions` accepts `null` (or empty) for an open end; it is stored as
  `0.0.0` / `9999.9999.9999` and `GET /api/versions` returns it as `null` again.
- `GET /api/logs` needs a pass: an admin gets every log, anyone else their own (the username comes
  from the pass). `POST /api/logs` without a pass only while `LEGACY_OPEN_READS` is on; with a pass
  the username is the pass's own.
