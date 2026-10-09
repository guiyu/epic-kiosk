import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock
from app.services.captcha.solver import HCaptchaChallengerSolver, TwoCaptchaTokenSolver, CaptchaSolveStatus

class ManualCaptchaTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_solver_does_not_start_challenge_processing(self):
        os.environ['EPIC_CAPTCHA_MODE']='manual'
        agent=AsyncMock()
        result=await HCaptchaChallengerSolver(agent).solve()
        self.assertEqual(result.status,CaptchaSolveStatus.MANUAL_REQUIRED)
        agent.wait_for_challenge.assert_not_called()

    async def test_external_solver_does_not_send_captcha_data(self):
        os.environ['EPIC_CAPTCHA_MODE']='manual'
        session=Mock()
        result=await TwoCaptchaTokenSolver(api_key='fixture',site_key='fixture',page_url='https://example.com',session=session).solve()
        self.assertEqual(result.status,CaptchaSolveStatus.MANUAL_REQUIRED)
        self.assertEqual(session.mock_calls,[])

if __name__=='__main__': unittest.main()
