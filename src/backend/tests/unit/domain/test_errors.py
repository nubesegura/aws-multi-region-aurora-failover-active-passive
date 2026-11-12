from dr_demo.domain.errors import (
    DatabaseReadOnly,
    DatabaseUnavailable,
    DomainError,
    FieldError,
    ValidationError,
)


def test_validation_error_carries_its_fields() -> None:
    error = ValidationError([FieldError("amount", "must be positive")])

    assert error.fields == (FieldError("amount", "must be positive"),)
    assert isinstance(error, DomainError)


def test_database_errors_are_domain_errors() -> None:
    assert issubclass(DatabaseReadOnly, DomainError)
    assert issubclass(DatabaseUnavailable, DomainError)
