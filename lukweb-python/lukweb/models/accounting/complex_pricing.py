import logging
import re
from collections import namedtuple
from typing import Optional, Iterable

from django.conf import settings
from django.db import models
from djmoney.forms import MoneyField
from moneyed import Money, Decimal
from django.utils.translation import (
    ugettext_lazy as _, pgettext_lazy
)

from .base import nonzero_money_validator, GnuCashCategory

logger = logging.getLogger(__name__)

__all__ =[
    'ActivityOption', 'PricingModel', 'PricingRule'
]

# TODO: //member/... rules
#  (//member/active //member/inactive, //member/<pk>, etc.)
# TODO: handle multiplicities?

# //pk/slug1/slug2/...
# pk's must refer to activities sharing the same payment formula
ROOTED_ACTIVITY_OPTION_PATH_PATTERN = re.compile(
    r'^(//(?P<act_ref>(\d+|self)))?(?P<comps>(/[-a-zA-Z0-9]+)+)$'
)

# [opt1, opt2, opt3] -> price "comment" <slug>
# comment/slug are optional
PRICING_RULE_CASE_PATTERN = re.compile(
    r'\[(?P<match_options>[-/,a-zA-Z0-9\s]*)\]\s*->\s*'
    r'(?P<price>(\d\d?)([,.]\d\d?)?)\s*'
    r'(\s\"(?P<comment>.*?)\")?\s*'
    r'(\s<(?P<filter_slug>[_-a-zA-Z0-9])>)?'
)

# pricing rules can then be considered as functions of sets of
# activity options (poss. spanning multiple activities)
# + some member data

# the payment backend supports arbitrary nesting etc., while the GUI
# only takes care of a very limited subset. This is both to reduce
# design complexity and to prevent admins from shooting themselves in the foot
# with options that are too complex to navigate.

class ActivityOption:

    def __init__(self, *, slug: str, parent: 'ActivityOption'=None,
                 bound=False, act_pk: Optional[int]=None):
        self.slug = slug
        self.parent = parent
        self.bound = bound
        self.act_pk = act_pk
        self._children = []
        assert (parent is None and not slug) or (parent is not None and slug)

    def validate(self):
        pass

    @property
    def is_root(self):
        return self.parent is None

    @property
    def children(self):
        return self._children

    @property
    def path(self):
        if self.is_root:
            return ''
        else:
            return '%s/%s' % (self.parent.path, self.slug)

    def bound_path(self):
        if not self.bound:
            raise ValueError('unbound')
        return '//%s%s' % (
            'self' if self.act_pk is None else self.act_pk,
            self.path
        )

    def __contains__(self, item):
        if not isinstance(item, ActivityOption):
            return False
        return self == item.parent or item.parent in self

    def __eq__(self, other):
        if not isinstance(other, ActivityOption):
            return False
        else:
            return (
                self.bound == other.bound and self.act_pk == other.act_pk
                and self.slug == other.slug and self.parent == other.parent
            )

    def __hash__(self):
        return hash((self.slug, self.parent, self.bound, self.act_pk))

    def __str__(self):
        return self.path

    def __repr__(self):
        return '<%s (%s)>' % (
            self.path,
            'bound:' + str(self.act_pk) if self.bound else 'unbound'
        )


class ActivityOptionRegistry:

    def __init__(self, focus: models.Model=None):
        self.seen = {}
        self.focus_pk = None if focus is None else focus.pk

    # general idea: the GUI thread initialises this by parsing option
    # declarations as UIActivityOption objects, while the payment processors
    # don't have to care about GUI stuff, so they can just work with base
    # ActivityOption objects
    def register(self, path, constructor=None, act_ref=None):
        if path and path != '/':
            cutoff = path.rfind('/')
            # shouldn't happen, since paths should start with /
            # but you never know
            if cutoff == -1:
                raise ValueError
            parent_path, path_base = path[:cutoff], path[cutoff + 1:]
            parent = self.ensure_registered(
                parent_path, constructor=constructor, act_ref=act_ref
            )
        else:
            path_base = ''
            parent = None
        act_ref = act_ref or self.focus_pk
        constructor = constructor or ActivityOption
        opt = constructor(
            slug=path_base, parent=parent, bound=act_ref is not None,
            act_pk=self.focus_pk
        )
        if parent is not None:
            parent._children.append(opt)
        self.seen[(path, act_ref)] = opt
        return opt

    def ensure_registered(self, path, constructor=None, act_ref=None):
        try:
            return self.seen[(path, act_ref)]
        except KeyError:
            return self.register(path, constructor=constructor, act_ref=act_ref)

    def __getitem__(self, item):
        m = ROOTED_ACTIVITY_OPTION_PATH_PATTERN.match(item)
        if m is None:
            raise ValueError
        act_ref = m.group('act_ref')
        if act_ref is None:
            act_ref = self.focus_pk
        act_ref = int(act_ref) if act_ref != 'self' else self.focus_pk
        path = m.group('comps')
        try:
            return self.seen[(path, act_ref)]
        except KeyError:
            raise KeyError(item)

    def __contains__(self, item):
        try:
            self.__getitem__(item)
            return True
        except KeyError:
            return False

    def __iter__(self):
        for (path, act_ref), option_obj in self.seen.items():
            yield path, option_obj

    @property
    def roots(self):
        for path, option_obj in self:
            if option_obj.is_root:
                yield option_obj

    def __repr__(self):
        return repr(self.seen)


class PricingModel(models.Model):

    name = models.CharField(
        max_length=150,
        verbose_name=_('name'),
    )

    enabled = models.BooleanField(
        verbose_name=_('pricing model enabled'),
        default=False,
        help_text=_(
            'While this flag is switched off, registrations for all activities '
            'using this pricing model will be disabled. It will still be '
            'available for selection in the admin interface in the meantime.'
        )
    )

    persist_active = models.BooleanField(
        verbose_name=_('persist \"active\" check'),
        default=False,
        help_text=_(
            'Treat all registrants of a registration from an active member '
            'as active. For pricing purposes, this is usually not what you '
            'want, but we leave in the option anyway.'
        )
    )

    class Meta:
        verbose_name = _('pricing model')
        verbose_name_plural = _('pricing models')


PricingData = namedtuple('PricingData', [
        'price', 'comment', 'filter_slug'
    ]
)

class PricingRule(models.Model):
    SCOPE_ACTIVE_ONLY = 1
    SCOPE_INACTIVE_ONLY = 2
    SCOPE_ALL_MEMBERS = 3

    SCOPE_CHOICES = (
        (
            SCOPE_ACTIVE_ONLY,
            pgettext_lazy(
                'activity target audience', 'Active members only'
            )
        ),
        (
            SCOPE_INACTIVE_ONLY,
            pgettext_lazy(
                'activity target audience', 'Inactive members only'
            )
        ),
        (
            SCOPE_ALL_MEMBERS,
            pgettext_lazy(
                'activity target audience', 'All members'
            )
        ),
    )

    pricing_model = models.ForeignKey(
        PricingModel,
        verbose_name=_('pricing model'),
        on_delete=models.CASCADE,
        related_name='rules'
    )

    description = models.CharField(
        max_length=150,
        verbose_name=_('description'),
        help_text=_(
            'Description of the item being priced. '
            'Also doubles as a default template for comments attached to '
            'debts associated with this pricing rule.'
        )
    )

    default_filter_slug = models.SlugField(
        verbose_name=_('default filter slug'),
        help_text=_(
            'Default filter slug to assign to debts associated with this '
            'pricing rule.'
        ),
        null=True,
        blank=True
    )

    gnucash_category = models.ForeignKey(
        GnuCashCategory,
        verbose_name=_('GnuCash category'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    no_match_default = MoneyField(
        verbose_name=_('default price when no match'),
        decimal_places=2,
        max_digits=6,
        default_currency=settings.BOOKKEEPING_CURRENCY,
        validators=[nonzero_money_validator],
        default=Money(0, settings.BOOKKEEPING_CURRENCY)
    )

    # TODO: user-friendly validator for this field
    specification = models.TextField(
        null=False,
        blank=True,
        verbose_name=_('Specification'),
        help_text=_(
            'Specify pricing rules for this item. '
            'Please refer to the manual for details.'
        )
    )

    scope = models.PositiveSmallIntegerField(
        verbose_name=_('Rule scope'),
        help_text=_(
            'Members to which this payment rule applies'
        ),
        choices=SCOPE_CHOICES,
        default=SCOPE_ALL_MEMBERS
    )

    count_multiple = models.BooleanField(
        verbose_name=_('Count with multiplicity'),
        help_text=_(
            'If unchecked, this rule will only be triggered once per '
            'registration. If checked, it will be applied to all additional '
            'registrant in accordance with the multiple registration pricing '
            'principles set out in the manual.'
        ),
        default=True
    )

    _relevant_activities = None
    _matching_rules = None

    def _parse_specification(self, registry: ActivityOptionRegistry):
        spec_lines = self.specification.split('\n')
        _relevant_activities = set()

        def handle_option(line_no, option):
            try:
                res = registry.ensure_registered(option)
            except ValueError:
                raise ValueError(
                    '%s is not a valid option (line %d)' % (option, line_no)
                )
            _relevant_activities.add(res.act_pk)
            return res

        def handle_line(line_no, line):
            m = PRICING_RULE_CASE_PATTERN.match(line.strip())
            if m is None:
                raise ValueError(
                    '%s does not constitute a valid pricing rule (line %d)'
                        % (line, line_no)
                )
            option_list_str = m.group('match_options').strip()
            if not option_list_str:
                options_to_parse = []
            else:
                options_to_parse = option_list_str.split(',')
            price = Decimal(m.group('price'))
            comment = m.group('comment') or self.description
            filter_slug = m.group('filter_slug') or self.default_filter_slug

            option_list = [
                handle_option(line_no, option) for option in options_to_parse
            ]
            return option_list, PricingData(
                price=price, comment=comment, filter_slug=filter_slug
            )

        self._matching_rules = [handle_line(*t) for t in enumerate(spec_lines)]
        self._relevant_activities = _relevant_activities

    @property
    def relevant_activity_pks(self):
        if self._relevant_activities is None:
            raise ValueError('Pricing rule has not been processed yet')
        return self._relevant_activities


    def opts_match(self, opts: Iterable[ActivityOption]) -> PricingData:
        if self._matching_rules is None:
            raise ValueError('Pricing rule has not been processed yet')

        def is_matched(criterium):
            return any(opt in criterium for opt in opts)

        for criteria, pricing_data in self._matching_rules:
            # a rule matches if *all* its criteria are satisfied
            if all(is_matched(cr) for cr in criteria):
                return pricing_data

        return PricingData(
            price=self.no_match_default, comment=self.description,
            filter_slug=self.default_filter_slug
        )


    class Meta:
        verbose_name = _('pricing rule')
        verbose_name_plural = _('pricing rules')