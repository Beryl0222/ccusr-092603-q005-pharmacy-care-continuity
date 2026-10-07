"""药店健康陪伴责任链领域契约。

- ``validate_event`` / ``ContractIssue``：交换层契约校验（信封、枚举、时间、版本、必填载荷）。
- ``ResponsibilityChain`` / ``rebuild`` / ``ChainError``：上层责任链业务服务。
"""

from .chain import ChainError, ResponsibilityChain, rebuild
from .contracts import ContractIssue, validate_event

__all__ = [
    "ContractIssue",
    "validate_event",
    "ChainError",
    "ResponsibilityChain",
    "rebuild",
]
