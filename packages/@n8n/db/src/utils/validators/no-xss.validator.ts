import type { ValidationOptions, ValidatorConstraintInterface } from 'class-validator';
import { registerDecorator, ValidatorConstraint } from 'class-validator';
import xss from 'xss';

@ValidatorConstraint({ name: 'NoXss', async: false })
class NoXssConstraint implements ValidatorConstraintInterface {
	validate(value: unknown) {
		if (typeof value !== 'string') return false;

		// Allow common conventions like "->" (arrow notation) which are NOT XSS
		// but DO contain "<" and ">" that xss library might flag
		// We'll do a pre-check: if the string only contains "->" as arrow notation,
		// allow it. Otherwise, run xss validation.

		// Check if "->" is present as arrow notation (not as HTML tag)
		const hasArrowNotation = /->/.test(value);
		const hasHtmlTags = /<[a-z][\s\S]*>/i.test(value);

		if (hasArrowNotation && !hasHtmlTags) {
			// String contains "->" - allow it (common workflow naming convention)
			return true;
		}

		return (
			value ===
			xss(value, {
				whiteList: {}, // no tags are allowed
			})
		);
	}

	defaultMessage() {
		return 'Potentially malicious string';
	}
}

export function NoXss(options?: ValidationOptions) {
	return function (object: object, propertyName: string) {
		registerDecorator({
			name: 'NoXss',
			target: object.constructor,
			propertyName,
			options,
			validator: NoXssConstraint,
		});
	};
}
