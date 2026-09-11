import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from cryptography.fernet import Fernet

from miriam_agent.core.exceptions import ValidationError, SecurityError

logger = logging.getLogger(__name__)

class InputValidator:
    """Input validation and sanitization for Miriam Financial Agent."""

    def __init__(self):
        self.fernet = Fernet(self._get_encryption_key())
        self.patterns = self._load_validation_patterns()
        self.blocked_keywords = self._load_blocked_keywords()
        self.allowed_characters = self._load_allowed_characters()

    def _get_encryption_key(self) -> str:
        """Get encryption key from environment or generate one."""
        # In production, this should come from a secure configuration
        # For development, we'll generate a key
        import os

        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            # Generate a key for development
            key = Fernet.generate_key().decode()
        return key

    def _load_validation_patterns(self) -> Dict[str, Any]:
        """Load validation patterns for different input types."""
        return {
            "amount": r"^\$?\s*(\d+(?:\.\d{2})?)(?:\s*(USD|dollars?)?)?$",
            "email": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
            "phone": r"^\+?1?-?\.?\s*(\(?\d{3}\)?)\s?(\d{3})\s?(\d{4})$",
            "account_number": r"^[A-Z0-9]{16,20}$",
            "routing_number": r"^\d{9}$",
            "crypto_address": r"^(0x)?[a-fA-F0-9]{40}$",
            "date": r"^\d{4}-\d{2}-\d{2}$",
            "time": r"^\d{2}:\d{2}(:\d{2})?$",
            "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            "symbol": r"^[A-Z]{1,5}$",
            "description": r"^[a-zA-Z0-9\s\-.,!?@#$%&*():;]{1,200}$",
            "username": r"^[a-zA-Z0-9_]{3,30}$",
            "password": r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$",
            "json_string": r"^{.*}$",
        }

    def _load_blocked_keywords(self) -> List[str]:
        """Load blocked keywords for security."""
        return [
            "select",
            "insert",
            "update",
            "delete",
            "drop",
            "union",
            "exec",
            "script",
            "vba",
            "macro",
            "command",
            "shell",
            "powershell",
            "bash",
            "rm",
            "del",
            "format",
            "dd",
            "mv",
            "cp",
            "sudo",
            "su",
            "sudo",
            "whoami",
            "id",
            "uname",
            "system",
            "eval",
            "import",
            "export",
            "env",
            "set",
            "clear",
            "ls",
            "cat",
            "grep",
            "find",
            "ps",
            "kill",
            "netstat",
            "ssh",
            "ftp",
            "telnet",
            "http",
            "https",
            "ftp://",
            "mailto:",
            "file://",
            "javascript:",
            "data:",
            "vbscript:",
            "onload",
            "onerror",
            "onclick",
            "onmouseover",
            "onfocus",
            "onblur",
            "onsubmit",
            "onreset",
            "onselect",
            "onchange",
            "onunload",
            "<script>",
            "</script>",
            "<iframe>",
            "<object>",
            "<embed>",
            "<applet>",
            "<meta>",
            "<link>",
            "<style>",
            "<form>",
            "<input>",
            "<button>
            "<select>",
            "<textarea>",
            "<label>",
            "<div>",
            "<span>",
            "<p>",
            "<h1>",
            "<h2>",
            "<h3>",
            "<h4>",
            "<h5>",
            "<h6>",
            "<table>",
            "<tr>",
            "<td>",
            "<th>",
            "<thead>",
            "<tbody>",
            "<tfoot>",
            "<ul>",
            "<ol>",
            "<li>",
            "<dl>",
            "<dt>",
            "<dd>",
            "<menu>",
            "<dir>",
            "<blockquote>",
            "<pre>",
            "<code>",
            "<samp>",
            "<kbd>",
            "<em>",
            "<strong>",
            "<small>",
            "<sub>",
            "<sup>",
            "<tt>",
            "<i>",
            "<b>",
            "<u>",
            "<s>",
            "<strike>",
            "<del>",
            "<ins>",
            "<mark>",
            "<cite>",
            "<q>",
            "<time>",
            "<abbr>",
            "<acronym>",
            "<base>",
            "<br>",
            "<col>",
            "<colgroup>",
            "<hr>",
            "<input>",
            "<keygen>",
            "<label>",
            "<legend>",
            "<meter>",
            "<param>",
            "<progress>",
            "<rp>",
            "<rt>",
            "<ruby>",
            "<source>",
            "<track>",
            "<wbr>",
        ]

    def _load_allowed_characters(self) -> str:
        """Load allowed characters for input validation."""
        return r"^[a-zA-Z0-9\s\-.,!?@#$%&*():;\\'"]+$"

    async def validate_user_input(
        self, input_data: Dict[str, Any], context: str = "general"
    ) -> Tuple[bool, List[str]]:
        """Validate user input for security."""
        try:
            errors = []

            # Validate each field based on context
            for field_name, field_value in input_data.items():
                field_errors = await self._validate_field(
                    field_name, field_value, context
                )
                errors.extend(field_errors)

            return len(errors) == 0, errors

        except Exception as e:
            logger.error(
                "Error validating user input",
                error=str(e),
                exc_info=True,
            )
            return False, [f"Input validation error: {str(e)}"]

    async def _validate_field(
        self, field_name: str, value: Any, context: str
    ) -> List[str]:
        """Validate a single field."""
        errors = []

        if value is None:
            return errors

        # Type validation
        if not isinstance(value, (str, int, float, dict, list)):
            errors.append(f"Field {field_name} has invalid type")
            return errors

        # Skip empty strings
        if isinstance(value, str) and not value.strip():
            return errors

        # Check for blocked keywords
        if isinstance(value, str):
            blocked_found = await self._check_blocked_keywords(value)
            if blocked_found:
                errors.append(
                    f"Field {field_name} contains blocked content"
                )

        # Validate based on field type and context
        if field_name == "amount" or context == "transaction":
            amount_errors = self._validate_amount(value)
            errors.extend(amount_errors)

        elif field_name == "email" or "email" in context:
            email_errors = self._validate_email(value)
            errors.extend(email_errors)

        elif field_name == "description" or field_name == "memo":
            description_errors = self._validate_description(value)
            errors.extend(description_errors)

        elif field_name == "account_number":
            account_errors = self._validate_account_number(value)
            errors.extend(account_errors)

        elif field_name == "routing_number":
            routing_errors = self._validate_routing_number(value)
            errors.extend(routing_errors)

        elif field_name == "crypto_address":
            crypto_errors = self._validate_crypto_address(value)
            errors.extend(crypto_errors)

        elif field_name == "date":
            date_errors = self._validate_date(value)
            errors.extend(date_errors)

        elif field_name == "time":
            time_errors = self._validate_time(value)
            errors.extend(time_errors)

        elif field_name == "username":
            username_errors = self._validate_username(value)
            errors.extend(username_errors)

        elif field_name == "password":
            password_errors = self._validate_password(value)
            errors.extend(password_errors)

        elif field_name == "symbol":
            symbol_errors = self._validate_symbol(value)
            errors.extend(symbol_errors)

        # General string validation
        elif isinstance(value, str):
            string_errors = self._validate_string(value, field_name)
            errors.extend(string_errors)

        return errors

    async def _check_blocked_keywords(self, text: str) -> bool:
        """Check if text contains blocked keywords."""
        text_lower = text.lower()
        for keyword in self.blocked_keywords:
            if keyword.lower() in text_lower:
                return True
        return False

    def _validate_amount(self, value: Any) -> List[str]:
        """Validate monetary amount."""
        errors = []

        if isinstance(value, (int, float)):
            if value < 0:
                errors.append("Amount cannot be negative")
            if value > 1000000000:  # 1 billion
                errors.append("Amount exceeds maximum allowed")
            if len(str(value).split('.')[-1]) > 2:  # More than 2 decimal places
                errors.append("Amount cannot have more than 2 decimal places")

        elif isinstance(value, str):
            # Try to parse string amount
            match = re.match(r"^\$?\s*(\d+(?:\.\d{2})?)", value)
            if not match:
                errors.append("Invalid amount format")
            else:
                amount = float(match.group(1))
                if amount < 0:
                    errors.append("Amount cannot be negative")
                if amount > 1000000000:
                    errors.append("Amount exceeds maximum allowed")

        return errors

    def _validate_email(self, value: str) -> List[str]:
        """Validate email address."""
        errors = []

        if not re.match(self.patterns["email"], value):
            errors.append("Invalid email format")

        # Check for disposable email domains
        disposable_domains = [
            "tempmail.com",
            "mailinator.com",
            "guerrillamail.com",
            "trashmail.com",
        ]

        domain = value.split("@")[-1]
        if domain in disposable_domains:
            errors.append("Disposable email addresses are not allowed")

        return errors

    def _validate_description(self, value: str) -> List[str]:
        """Validate description field."""
        errors = []

        if len(value) < 1:
            errors.append("Description cannot be empty")
        if len(value) > 200:
            errors.append("Description cannot exceed 200 characters")

        # Check for suspicious patterns in description
        suspicious_patterns = [
            r"<script>",
            r"javascript:",
            r"data:",
            r"vbscript:",
            r"onload=",
            r"onerror=",
        ]

        for pattern in suspicious_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                errors.append("Description contains suspicious content")
                break

        return errors

    def _validate_account_number(self, value: str) -> List[str]:
        """Validate account number."""
        errors = []

        if not re.match(self.patterns["account_number"], value):
            errors.append("Invalid account number format")

        return errors

    def _validate_routing_number(self, value: str) -> List[str]:
        """Validate routing number."""
        errors = []

        if not re.match(self.patterns["routing_number"], value):
            errors.append("Invalid routing number format")

        return errors

    def _validate_crypto_address(self, value: str) -> List[str]:
        """Validate cryptocurrency address."""
        errors = []

        if not re.match(self.patterns["crypto_address"], value):
            errors.append("Invalid cryptocurrency address format")

        return errors

    def _validate_date(self, value: str) -> List[str]:
        """Validate date format."""
        errors = []

        if not re.match(self.patterns["date"], value):
            errors.append("Invalid date format. Use YYYY-MM-DD")

        # Check if date is valid
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            errors.append("Invalid date")

        return errors

    def _validate_time(self, value: str) -> List[str]:
        """Validate time format."""
        errors = []

        if not re.match(self.patterns["time"], value):
            errors.append("Invalid time format. Use HH:MM or HH:MM:SS")

        return errors

    def _validate_username(self, value: str) -> List[str]:
        """Validate username."""
        errors = []

        if not re.match(self.patterns["username"], value):
            errors.append(
                "Username must be 3-30 characters and contain only letters, numbers, and underscores"
            )

        return errors

    def _validate_password(self, value: str) -> List[str]:
        """Validate password strength."""
        errors = []

        if not re.match(self.patterns["password"], value):
            errors.append(
                "Password must be at least 8 characters and contain uppercase, lowercase, number, and special character"
            )

        return errors

    def _validate_symbol(self, value: str) -> List[str]:
        """Validate stock symbol."""
        errors = []

        if not re.match(self.patterns["symbol"], value):
            errors.append(
                "Invalid symbol format. Use 1-5 uppercase letters"
            )

        return errors

    def _validate_string(self, value: str, field_name: str) -> List[str]:
        """Validate general string field."""
        errors = []

        # Check length
        if len(value) < 1:
            errors.append(f"Field {field_name} cannot be empty")
        elif len(value) > 200:
            errors.append(f"Field {field_name} cannot exceed 200 characters")

        # Check for allowed characters
        if not re.match(self.allowed_characters, value):
            errors.append(
                f"Field {field_name} contains invalid characters"
            )

        return errors

    async def sanitize_input(
        self, input_data: Dict[str, Any], context: str = "general"
    ) -> Dict[str, Any]:
        """Sanitize user input to prevent injection attacks."""
        try:
            sanitized_data = {}

            for field_name, field_value in input_data.items():
                if isinstance(field_value, str):
                    sanitized_data[field_name] = await self._sanitize_string(
                        field_value, field_name, context
                    )
                elif isinstance(field_value, dict):
                    sanitized_data[field_name] = await self._sanitize_dict(
                        field_value, context
                    )
                elif isinstance(field_value, list):
                    sanitized_data[field_name] = await self._sanitize_list(
                        field_value, context
                    )
                else:
                    sanitized_data[field_name] = field_value

            return sanitized_data

        except Exception as e:
            logger.error(
                "Error sanitizing input",
                error=str(e),
                exc_info=True,
            )
            return input_data

    async def _sanitize_string(
        self, value: str, field_name: str, context: str
    ) -> str:
        """Sanitize a string value."""
        try:
            # Remove or escape potentially dangerous characters
            sanitized = value

            # Escape HTML special characters
            sanitized = sanitized.replace("&", "&amp;")
            sanitized = sanitized.replace("<", "&lt;")
            sanitized = sanitized.replace(">", "&gt;")
            sanitized = sanitized.replace('"', "&quot;")
            sanitized = sanitized.replace("'", "&#x27;")

            # Remove script tags and JavaScript
            sanitized = re.sub(r"<script[^>]*>.*?</script>", "", sanitized, flags=re.IGNORECASE)
            sanitized = re.sub(r"javascript:", "", sanitized, flags=re.IGNORECASE)
            sanitized = re.sub(r"vbscript:", "", sanitized, flags=re.IGNORECASE)

            # Remove SQL injection patterns
            sql_patterns = [
                r"select\s",
                r"insert\s",
                r"update\s",
                r"delete\s",
                r"drop\s",
                r"union\s",
                r"exec\s",
                r"script\s",
                r"vba\s",
                r"macro\s",
                r"command\s",
                r"shell\s",
                r"powershell\s",
                r"bash\s",
                r"rm\s",
                r"del\s",
                r"format\s",
                r"dd\s",
                r"mv\s",
                r"cp\s",
                r"sudo\s",
                r"su\s",
                r"whoami\s",
                r"id\s",
                r"uname\s",
                r"system\s",
                r"eval\s",
                r"import\s",
                r"export\s",
                r"env\s",
                r"set\s",
                r"clear\s",
                r"ls\s",
                r"cat\s",
                r"grep\s",
                r"find\s",
                r"ps\s",
                r"kill\s",
                r"netstat\s",
                r"ssh\s",
                r"ftp\s",
                r"telnet\s",
                r"http\s",
                r"https\s",
            ]

            for pattern in sql_patterns:
                sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)

            # Apply context-specific sanitization
            if context == "transaction":
                sanitized = await self._sanitize_transaction_description(sanitized)

            return sanitized.strip()

        except Exception as e:
            logger.error(
                "Error sanitizing string",
                error=str(e),
                exc_info=True,
            )
            return value

    async def _sanitize_dict(
        self, value: Dict[str, Any], context: str
    ) -> Dict[str, Any]:
        """Sanitize dictionary."""
        sanitized = {}
        for key, val in value.items():
            if isinstance(val, str):
                sanitized[key] = await self._sanitize_string(
                    val, key, context
                )
            elif isinstance(val, dict):
                sanitized[key] = await self._sanitize_dict(val, context)
            elif isinstance(val, list):
                sanitized[key] = await self._sanitize_list(val, context)
            else:
                sanitized[key] = val

        return sanitized

    async def _sanitize_list(
        self, value: List[Any], context: str
    ) -> List[Any]:
        """Sanitize list."""
        sanitized = []
        for item in value:
            if isinstance(item, str):
                sanitized.append(
                    await self._sanitize_string(item, "list_item", context)
                )
            elif isinstance(item, dict):
                sanitized.append(await self._sanitize_dict(item, context))
            elif isinstance(item, list):
                sanitized.append(await self._sanitize_list(item, context))
            else:
                sanitized.append(item)

        return sanitized

    async def _sanitize_transaction_description(self, description: str) -> str:
        """Sanitize transaction description."""
        # Remove or flag suspicious descriptions
        suspicious_patterns = [
            r"\$.*\$",  # Multiple dollar signs
            r"^[a-zA-Z0-9]{20,}$",  # Very long alphanumeric
            r"\d{16}\s*\d{2}\s*\d{4}",  # Credit card number pattern
        ]

        for pattern in suspicious_patterns:
            if re.search(pattern, description):
                description = f"[REVIEWED] {description}"
                break

        return description

    async def encrypt_sensitive_data(self, data: str) -> str:
        """Encrypt sensitive data."""
        try:
            encrypted_data = self.fernet.encrypt(data.encode())
            return encrypted_data.decode()
        except Exception as e:
            logger.error(
                "Error encrypting sensitive data",
                error=str(e),
                exc_info=True,
            )
            raise SecurityError("Failed to encrypt sensitive data")

    async def decrypt_sensitive_data(self, encrypted_data: str) -> str:
        """Decrypt sensitive data."""
        try:
            decrypted_data = self.fernet.decrypt(encrypted_data.encode())
            return decrypted_data.decode()
        except Exception as e:
            logger.error(
                "Error decrypting sensitive data",
                error=str(e),
                exc_info=True,
            )
            raise SecurityError("Failed to decrypt sensitive data")

    async def generate_secure_token(self, length: int = 32) -> str:
        """Generate a secure token."""
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(length))
        return token

    async def validate_password_strength(self, password: str) -> Tuple[bool, List[str]]:
        """Validate password strength."""
        errors = []

        if len(password) < 8:
            errors.append("Password must be at least 8 characters")

        if not re.search(r'[a-z]', password):
            errors.append("Password must contain at least one lowercase letter")

        if not re.search(r'[A-Z]', password):
            errors.append("Password must contain at least one uppercase letter")

        if not re.search(r'\d', password):
            errors.append("Password must contain at least one number")

        if not re.search(r'[@$!%*?&]', password):
            errors.append("Password must contain at least one special character")

        # Check against common passwords
        common_passwords = [
            "password", "12345678", "qwerty", "admin", "letmein",
            "welcome", "monkey", "password123", "123456789"
        ]

        if password.lower() in common_passwords:
            errors.append("Password is too common")

        return len(errors) == 0, errors

    async def validate_rate_limit(self, user_id: str, action: str) -> bool:
        """Validate rate limiting for user actions."""
        try:
            # This would typically use Redis or another rate limiting store
            # For now, return True (no rate limiting)
            return True

        except Exception as e:
            logger.error(
                "Error validating rate limit",
                error=str(e),
                exc_info=True,
            )
            return False

    async def check_for_malware_urls(self, url: str) -> Tuple[bool, List[str]]:
        """Check if URL contains malware."""
        try:
            # This would typically use a threat intelligence API
            # For now, return False (no malware detection)
            return False, []

        except Exception as e:
            logger.error(
                "Error checking for malware URLs",
                error=str(e),
                exc_info=True,
            )
            return True, ["URL security check failed"]

    async def validate_content_filtering(self, content: str) -> Tuple[bool, List[str]]:
        """Validate content filtering."""
        try:
            # Check for spam or malicious content
            spam_patterns = [
                r"click here",
                r"urgent",
                r"immediate",
                r"limited time",
                r"act now",
                r"guaranteed",
                r"free money",
                r"quick rich",
                r"work from home",
                r"earn extra cash",
            ]

            violations = []
            for pattern in spam_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    violations.append(f"Content violates policy: {pattern}")

            return len(violations) == 0, violations

        except Exception as e:
            logger.error(
                "Error validating content filtering",
                error=str(e),
                exc_info=True,
            )
            return True, ["Content filtering check failed"]
