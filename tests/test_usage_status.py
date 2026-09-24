import unittest
from usage_status import format_usage


class UsageFormatTests(unittest.TestCase):
    def test_current_buckets_preferred_and_remaining_not_used(self):
        text=format_usage({'rateLimits':{'primary':{'usedPercent':1}},'rateLimitsByLimitId':{
            'codex':{'primary':{'usedPercent':95,'windowDurationMins':10080,'resetsAt':1789353430}},
            'spark':{'limitName':'Spark','primary':{'usedPercent':0,'windowDurationMins':300}}}})
        self.assertIn('Weekly: 5% remaining',text)
        self.assertNotIn('Spark',text)
        self.assertNotIn('5-hour: 100% remaining',text)
        self.assertIn('<t:1789353430:f>',text)
        self.assertNotIn('99% remaining',text)

    def test_null_usage_and_reset_are_unknown_not_zero(self):
        text=format_usage({'rateLimits':{'primary':{'usedPercent':None,'resetsAt':None}}})
        self.assertIn('remaining allowance unavailable',text)
        self.assertIn('reset time unavailable',text)
        self.assertNotIn('100% remaining',text)

    def test_legacy_clamp_and_empty_response(self):
        text=format_usage({'rateLimits':{'primary':{'usedPercent':105}}})
        self.assertIn('0% remaining',text)
        self.assertIn('Usage information unavailable',format_usage({}))


if __name__=='__main__': unittest.main()
