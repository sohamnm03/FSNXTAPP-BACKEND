# `/api/users` Frontend Integration Handoff

This document describes the current backend contract for listing, creating, and
updating users.

## Permission fields

The user API exposes these access fields:

| Field | Meaning |
| --- | --- |
| `isActive` | Whether the account is active. |
| `isDev` | Development access. |
| `isTesting` | Testing access. |
| `isConfig` | Configuration access. |
| `isFsNxt` | FSNXT access. |
| `isSapFsNxt` | SAP FSNXT access. |

When sending a user, every permission field is required and may be either a
JSON boolean (`true`/`false`) or an integer (`1`/`0`). Responses normalize the
five module permission fields to integers. The POST response also normalizes
`isActive` to an integer.

## List users

### Request

```http
GET /api/users
Accept: application/json
```

No request body is needed.

### Successful response

Status: `200 OK`

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

Users are returned newest first, ordered by `created_at` and then database ID.
Dates are serialized as ISO-8601 strings when supplied by the database as date
objects. A date may be `null` if its database value is null.

The legacy `modules` array currently includes only these mappings:

| Permission | `modules` value |
| --- | --- |
| `isDev = 1` | `"development"` |
| `isTesting = 1` | `"testing"` |

Use the explicit `isConfig`, `isFsNxt`, and `isSapFsNxt` fields for those
permissions; they are not represented in `modules`.

### Failure response

Status: `503 Service Unavailable`

```json
{
  "success": false,
  "message": "User service is unavailable."
}
```

## Create or update a user

The same endpoint performs both operations.

### Request

```http
POST /api/users
Content-Type: application/json
```

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

`fullName` is accepted as an alternative to `full_name`, but new frontend code
should use `full_name` to match the response field.

### Create versus update behavior

The backend performs a case-insensitive lookup by email:

- If no matching email exists, it inserts a new user and returns `201`.
- If a matching email exists, it replaces that user's username, email, full
  name, active status, and all five permission values, then returns `200`.

This is an upsert operation. The frontend does not need separate create and
update URLs. Changing the email to one not already in the database creates a
new record rather than renaming the existing record.

### Successful create response

Status: `201 Created`

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

### Successful update response

Status: `200 OK`

The response has the same `user` shape as the create response, with the update
message:

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

## Validation rules

| Field | Rule |
| --- | --- |
| `username` | Required non-empty string; maximum 100 characters. |
| `email` | Required non-empty string; maximum 255 characters. |
| `full_name` | Required non-empty string; maximum 255 characters. |
| All permission fields | Required; must be `true`, `false`, `1`, or `0`. |

Leading and trailing whitespace is removed from `username`, `email`, and
`full_name`. The endpoint currently checks that email is non-empty and within
the length limit, but it does not validate email syntax.

### Validation failure

Status: `400 Bad Request`

Examples of response messages include:

```json
{
  "success": false,
  "message": "username, email, and full_name are required."
}
```

```json
{
  "success": false,
  "message": "isActive, isDev, isTesting, isConfig, isFsNxt, and isSapFsNxt must each be true, false, 0, or 1."
}
```

Other possible failures:

| Status | Meaning |
| --- | --- |
| `400` | Invalid JSON body, missing field, invalid permission value, or a string exceeds its maximum length. |
| `409` | The submitted data conflicts with an existing database record. |
| `503` | The database/user service is unavailable. |

Frontend code should use the HTTP status for control flow and display the
backend `message` when appropriate.

## TypeScript types

```ts
type BinaryFlag = boolean | 0 | 1;

export interface SaveUserRequest {
  username: string;
  email: string;
  full_name: string;
  isActive: BinaryFlag;
  isDev: BinaryFlag;
  isTesting: BinaryFlag;
  isConfig: BinaryFlag;
  isFsNxt: BinaryFlag;
  isSapFsNxt: BinaryFlag;
}

export interface UserPermissions {
  isDev: 0 | 1;
  isTesting: 0 | 1;
  isConfig: 0 | 1;
  isFsNxt: 0 | 1;
  isSapFsNxt: 0 | 1;
}

export interface SavedUser extends UserPermissions {
  id: number;
  username: string;
  email: string;
  full_name: string;
  isActive: 0 | 1;
}

export interface ListedUser extends UserPermissions {
  username: string;
  email: string;
  full_name: string;
  isActive: 0 | 1 | boolean;
  modules: string[];
  created_at: string | null;
  updated_at: string | null;
}
```

## Fetch examples

```ts
const API_BASE_URL = "http://127.0.0.1:5000";

export async function getUsers(): Promise<ListedUser[]> {
  const response = await fetch(`${API_BASE_URL}/api/users`);
  const body = await response.json();

  if (!response.ok) {
    throw new Error(body.message ?? "Unable to load users.");
  }

  return body.users;
}

export async function saveUser(user: SaveUserRequest): Promise<SavedUser> {
  const response = await fetch(`${API_BASE_URL}/api/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(user),
  });
  const body = await response.json();

  if (!response.ok) {
    throw new Error(body.message ?? "Unable to save user.");
  }

  return body.user;
}
```

For checkbox inputs, send their boolean `checked` values directly. When reading
a response, either compare a flag to `1` or normalize with `Boolean(flag)`.

## Authentication and related responses

At present, `GET /api/users` and `POST /api/users` do not enforce a bearer token
inside the backend route. If the deployed environment protects them through a
gateway, the frontend must follow that gateway's authentication requirements.

The login responses and `GET /api/me` also expose `isFsNxt` and `isSapFsNxt`,
alongside `isDev`, `isTesting`, and `isConfig`. These can be used to build the
current user's navigation and route guards.

## Database prerequisite

The backend expects the MySQL `users` table to contain `isFsNxt` and
`isSapFsNxt` columns in addition to the existing permission columns. Deploying
this API code before those columns exist will cause user queries to fail.
