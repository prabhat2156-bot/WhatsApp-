import io.github.cdimascio.dotenv.Dotenv;

public class Main {

    public static void main(String[] args) {

        Dotenv dotenv = Dotenv.load();

        String token = dotenv.get("BOT_TOKEN");
        String username = dotenv.get("BOT_USERNAME");

        System.out.println("Bot Username: " + username);
        System.out.println("Bot Token Loaded Successfully!");

        // Yahan bot startup code add karo
    }
}
