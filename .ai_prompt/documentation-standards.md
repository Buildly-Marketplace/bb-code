# Documentation Standards

This document defines documentation requirements for bb-code following Buildly Forge standards.

## Documentation Types

### 1. Code Documentation

Every module, class, and function should have docstrings.

#### Module Docstrings

```python
"""
User authentication and session management.

This module handles local and OAuth authentication, session creation,
and user identity verification.
"""
```

#### Class Docstrings

```python
class UserService:
    """Service for user-related operations.
    
    Provides methods for creating, updating, and querying users.
    All methods are async and require a database session.
    
    Attributes:
        None (stateless service)
        
    Example:
        >>> service = UserService()
        >>> user = await service.get_by_email(db, "test@example.com")
    """
```

#### Function Docstrings (Google Style)

```python
async def send_notification(
    db: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    url: str | None = None,
) -> int:
    """Send push notification to a user.
    
    Sends a web push notification to all registered devices for the user.
    Invalid subscriptions are automatically cleaned up.
    
    Args:
        db: Database session.
        user_id: The recipient user's ID.
        title: Notification title (max 50 chars).
        body: Notification body text.
        url: Optional URL to open when clicked.
        
    Returns:
        Number of notifications successfully sent.
        
    Raises:
        ValueError: If title exceeds 50 characters.
        
    Example:
        >>> sent = await send_notification(db, 123, "New Message", "You have a message")
        >>> print(f"Sent {sent} notifications")
    """
```

### 2. API Documentation

Typer and local HTTP UI auto-generates OpenAPI docs. Enhance with:

#### Endpoint Descriptions

```python
@router.post(
    "/subscribe",
    summary="Subscribe to push notifications",
    description="""
    Register a push subscription for the current user.
    
    The subscription will be used to send web push notifications
    for new messages, mentions, and other events.
    """,
    response_description="Subscription status",
)
async def subscribe(
    request: Request,
    endpoint: Annotated[str, Form(description="Push service endpoint URL")],
    p256dh: Annotated[str, Form(description="P-256 ECDH public key")],
    auth: Annotated[str, Form(description="Authentication secret")],
):
    ...
```

#### Response Models

```python
from pydantic import BaseModel, Field

class UserResponse(BaseModel):
    """User profile response."""
    
    id: int = Field(description="Unique user ID")
    email: str = Field(description="User's email address")
    display_name: str = Field(description="Display name")
    avatar_url: str | None = Field(description="Avatar image URL")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": 1,
                "email": "alice@example.com",
                "display_name": "Alice",
                "avatar_url": "https://example.com/avatar.jpg"
            }
        }
```

### 3. README Files

Each major directory should have a README:

```
app/
├── README.md           # Application overview
├── models/
│   └── README.md       # Model documentation
├── routers/
│   └── README.md       # Router documentation
└── services/
    └── README.md       # Service documentation
```

### 4. Developer Documentation

Store in `devdocs/`:

```
devdocs/
├── README.md                    # Documentation index
├── architecture/
│   ├── overview.md              # System architecture
│   ├── data-models.md           # Database models
│   └── api-structure.md         # API organization
├── guides/
│   ├── getting-started.md       # Local setup
│   ├── testing.md               # Testing guide
│   └── migrations.md            # Database migrations
└── ops/
    ├── configuration.md         # Environment variables
    └── deployment.md            # Deployment guides
```

### 5. AI Instructions

Store in `.ai_prompt/`:

```
.ai_prompt/
├── README.md                    # AI instruction index
├── overview.md                  # Project overview for AI
├── coding-standards.md          # Code style for AI
├── testing-guidelines.md        # Testing requirements
├── data-management.md           # Database patterns
└── security.md                  # Security guidelines
```

## Documentation Requirements

### When to Update Documentation

| Change | Required Documentation |
|--------|----------------------|
| New endpoint | Docstring, OpenAPI tags |
| New model | Model docstring, data-models.md |
| New feature | README, relevant guide |
| Config change | configuration.md |
| Breaking change | CHANGELOG, README |

### Documentation Checklist

When making changes, verify:

- [ ] Functions have docstrings
- [ ] Classes have docstrings
- [ ] Complex logic has inline comments
- [ ] API endpoints have descriptions
- [ ] New features are documented in guides
- [ ] Configuration changes are documented

## Writing Guidelines

### Be Concise

```
# ✅ Good
Send push notification to user.

# ❌ Bad
This function is used to send a push notification to the specified user
by iterating through their registered push subscriptions and calling
the web push API for each one.
```

### Use Active Voice

```
# ✅ Good
Returns the user object.

# ❌ Bad
The user object is returned.
```

### Include Examples

```python
def parse_slug(text: str) -> str:
    """Convert text to URL-safe slug.
    
    Example:
        >>> parse_slug("Hello World!")
        'hello-world'
        >>> parse_slug("  Spaces  ")
        'spaces'
    """
```

### Document Exceptions

```python
def get_user_or_404(db: AsyncSession, user_id: int) -> User:
    """Get user by ID.
    
    Raises:
        HTTPException: 404 if user not found.
    """
```

## Markdown Standards

### Headings

Use proper hierarchy:

```markdown
# Page Title (H1 - one per page)

## Major Section (H2)

### Subsection (H3)

#### Minor Section (H4)
```

### Code Blocks

Always specify language:

````markdown
```python
def example():
    pass
```

```bash
pytest --cov=app
```

```sql
SELECT * FROM users WHERE email = 'test@example.com';
```
````

### Tables

Use for structured information:

```markdown
| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DEBUG` | bool | `false` | Enable debug mode |
```

### Links

Use relative links for internal docs:

```markdown
See [Testing Guide](./guides/testing.md) for more details.
```

## Keeping Documentation Current

### Review Triggers

- Code reviews should verify documentation
- Major releases should include doc review
- Quarterly documentation audits

### Version Documentation

For versioned APIs:

```markdown
## API v2 Changes (2026-01-15)

- Added `/api/v2/users` endpoint
- Deprecated `/api/v1/users` (removal in 3 months)
```

---

*Following these standards ensures the codebase remains maintainable and accessible to all contributors.*
