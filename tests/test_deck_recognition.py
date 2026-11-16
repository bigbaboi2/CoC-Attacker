"""Regression for selected troops and the ice spell in the main battle bar."""
import unittest
from pathlib import Path

import cv2
import numpy as np

from core.deck import DeckRecognizer
from core.vision import VisionEngine


class DeckRecognitionTests(unittest.TestCase):
    def test_selected_dragon_and_ice_spell_are_not_lost(self):
        strip = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'selected_main_deck.png'))
        screen = np.zeros((900, 1600, 3), dtype=np.uint8)
        screen[730:900, :800] = strip
        vision = VisionEngine()
        vision._get_ocr = lambda: object()
        deck = DeckRecognizer(vision)
        deck.read_count = lambda frame, x: 8 if x < 250 else 4 if x < 630 else 1

        cards = deck.scan(screen)

        self.assertEqual([card['name'] for card in cards['troops']], ['dragon'])
        self.assertEqual([card['name'] for card in cards['spells']], ['no', 'bang'])
        self.assertEqual([card['count'] for card in cards['spells']], [4, 1])


if __name__ == '__main__':
    unittest.main()
