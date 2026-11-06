import 'package:flutter_test/flutter_test.dart';
import 'package:netra/main.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('shows the single hold-to-talk capture control', (
    WidgetTester tester,
  ) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const NetraApp());

    expect(find.text('HOLD TO TALK'), findsOneWidget);
    expect(
      find.text('1× SCENE     ·     2× READ TEXT     ·     3× SOS'),
      findsOneWidget,
    );
  });
}
