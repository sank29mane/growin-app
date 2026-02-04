---
name: better-auth-best-practices
description: Integration guide for Better Auth, focusing on security and type-safety.
---

# Better Auth Best Practices

This skill provides instructions for implementing and maintaining Better Auth in modern web/mobile applications.

## Core Instructions

1. **Environment Config**: Always store secrets in `.env` and use `BETTER_AUTH_SECRET`.
2. **Schema Management**: Follow the standardized schema for user, session, and account tables. Use the CLI for migrations.
3. **Client-Side Auth**: Use the type-safe client for all session checks to ensure frontend/backend consistency.
4. **Social Logins**: Standardize OAuth flow handling and user merging patterns.
5. **Security**: Implement rate limiting on auth endpoints and use the recommended CORS settings for native app interactions.

## CLI Commands
- `npx better-auth generate`: Generate types and sync schema.
- `npx better-auth migrate`: Run database migrations.
