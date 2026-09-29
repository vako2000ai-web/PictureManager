class Cancelled(Exception):
    """Задача отменена пользователем."""


class IntegrityError(Exception):
    """Копия файла не совпала с оригиналом по SHA-256."""


class ChangedError(Exception):
    """Файл изменён вне приложения после построения плана или скана."""
