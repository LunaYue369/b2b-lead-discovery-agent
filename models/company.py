"""公司身份。

这个模块回答一个问题：两条数据说的是不是同一家公司。
normalize 的归并和 dedup 的查库都靠它，所以它必须只有一份实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 公司名归一化时要剥掉的法律后缀。只剥法律后缀：
# 剥 "Technologies" 会把 "North Field Technologies" 和 "North Field" 错并成一家。
_LEGAL_SUFFIXES = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "gmbh", "ag", "sa", "s.a.", "bv",
    "b.v.", "nv", "ab", "oy", "as", "pte", "pty", "plc", "sdn", "bhd", "kk",
    "k.k.", "srl", "s.r.l.", "spa", "s.p.a.",
}

_LINKEDIN_COMPANY_SLUG = re.compile(r"linkedin\.com/company/([^/?#]+)", re.IGNORECASE)


def normalize_company_name(name: str) -> str:
    """公司名归一化。身份键梯度里最弱的一级，只在前三级全落空时兜底。"""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s&-]", " ", s)  # 标点变空格，& 和 - 保留
    tokens = [t for t in s.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def linkedin_company_slug(url: str) -> str | None:
    """从 LinkedIn 公司页 URL 里抽 slug。抽不到返回 None，不猜。"""
    m = _LINKEDIN_COMPANY_SLUG.search(url)
    return m.group(1).lower().rstrip("/") if m else None


def normalize_domain(domain: str) -> str:
    """域名归一化：去协议、去 www、去尾斜杠、小写。"""
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].rstrip(".")


class IdentityUnresolvable(ValueError):
    """连公司名都没有，身份键建不出来。

    这个异常存在的意义是不给兜底：宁可显式炸掉让调用方记一条
    CandidateDropReason.NO_IDENTITY，也不要悄悄编一个键出来。
    编出来的键会在 dedup 阶段把两家不相干的公司并成一家。
    """


@dataclass(frozen=True)
class CompanyRef:
    """一家公司的身份。

    这里有三个 `| None`，看着违反「不加 Optional」那条规矩，说明一下：
    这不是内部字段偷懒，是外部数据的真实缺失 —— 实测 88% 的 post 作者
    （profile 类型）拿不到任何公司 id。规矩要守在别处：身份键梯度用尽
    还是拿不到键，就直接 raise，绝不用「名字凑一个」兜底。
    """

    name: str
    linkedin_company_id: str | None  # LinkedIn 数字 id，如 "100000001"，最稳的键
    linkedin_url: str | None  # 公司页 URL，slug 可能被改，比 id 弱一级
    domain: str | None  # 官网域名，enrich 阶段才可能有

    @property
    def identity_key(self) -> tuple[str, str]:
        """身份键梯度：LinkedIn 数字 id > 公司页 slug > 域名 > 归一化公司名。

        normalize 的归并和 dedup 的查库必须用同一个键，所以它放在这里
        而不是两边各实现一遍。两边实现漂了，症状是同一家公司在库里有两行，
        而且极难发现。
        """
        if self.linkedin_company_id:
            return ("li_id", self.linkedin_company_id)
        if self.linkedin_url:
            slug = linkedin_company_slug(self.linkedin_url)
            if slug:
                return ("li_slug", slug)
        if self.domain:
            return ("domain", normalize_domain(self.domain))
        key = normalize_company_name(self.name)
        if key:
            return ("name", key)
        raise IdentityUnresolvable(f"无法为 {self!r} 建立身份键")

    @property
    def is_weak_identity(self) -> bool:
        """身份只靠公司名撑着。dedup 命中要打折，analyzer 的 confidence 要降级。"""
        return self.identity_key[0] == "name"

    def merge(self, other: CompanyRef) -> CompanyRef:
        """合并两条指向同一家公司的引用，补齐彼此缺的字段。

        归并时一条 candidate 可能只有 slug、另一条只有 domain，
        合起来这家公司的身份才完整。
        """
        if self.identity_key != other.identity_key:
            raise ValueError(f"身份键不同，不能合并：{self.identity_key} vs {other.identity_key}")
        return CompanyRef(
            name=self.name or other.name,
            linkedin_company_id=self.linkedin_company_id or other.linkedin_company_id,
            linkedin_url=self.linkedin_url or other.linkedin_url,
            domain=self.domain or other.domain,
        )
