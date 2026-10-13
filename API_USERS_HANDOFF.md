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
